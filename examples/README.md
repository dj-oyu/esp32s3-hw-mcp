# PIE（EE.*）実用サンプル

ESP32-S3 の拡張命令セット（TRM の PIE / `EE.*`）を、**マニュアルの疑似コードから手書きしたアセンブリ**で
動かす例題集です。どの例も自分で自分の答え合わせをします:

1. カーネル（手書き PIE アセンブリ、`examples/firmware/main/ex*.S`）
2. ファーム内の C 参照実装（同じ疑似コードから独立に書いたスカラ版）— 装置上で比較
3. ホスト側の Python 参照実装（`tools/check_examples_log.py`、ログが持っている入力から再計算）— 三つ目の意見

`MANUAL → C → Python` の三つが一致したときだけ「わかった」と言えます。マニュアルとアセンブラが食い違う
箇所は一致を求めず、**どちらの文書にシリコンが従ったかを判定**して表示します（それが例題の成果物です）。

## 動かし方

```bash
# 1) ビルド（ESP-IDF v6.0.1。リポジトリ内の tools/build_examples.sh が export.sh を読む）
bash tools/build_examples.sh
# -> examples/firmware/build_examples/pie_examples.bin

# 2) 焼いてログを取る（WSL ホスト側。esptool と pyserial だけ必要、IDF は不要）
bash /workspace/esp32s3-hw-mcp/tools/host_flash_and_log.sh --examples
# -> /workspace/backups/pie-examples-<stamp>.log
#    同じ実行の中で tools/check_examples_log.py が走り、判定と report.json が出る

# 3) 手元で判定だけやり直す
.venv/bin/python tools/check_examples_log.py /workspace/backups/pie-examples-<stamp>.log
```

`--examples` は焼き込み先を `examples/firmware/build_examples` に切り替えるだけで、識別 → 退避 →
書き込み → 読み戻し sha256 照合 → リセットしてキャプチャ、という流れは計測用ファームと同じです。
元のファームに戻すときは `--restore /workspace/backups/cardputer-s3-<stamp>.bin`。

## 例題一覧

| 例 | 何を固めるか | 期待（出どころ） | 外れたときに分かること |
|---|---|---|---|
| `ex01` encoding | `EE.LD.ACCX.IP` / `EE.ST.ACCX.IP` / `EE.LD.128.USAR.IP` / `EE.VLD.128.IP` が**アドレスレジスタを何バイト進めるか**、`EE.SRS.ACCX` のシフトと飽和、`EE.BITREV` のオペランドの正体 | 生バイトで `field=1` を書いて実測。LD は疑似コードから 8、ST は文書が 3 通りに割れている（8 / 4 / 1） | どの文書が正しいかが決まる。`data/pie_encoding_errata.json` の 2 件が片付く |
| `ex02` matmul16 | `EE.VMULAS.S16.ACCX`（8 レーンの積を**合算**して ACCX に入る）＋ `EE.SRS.ACCX` で 16×16 の int16 行列積 | C 参照と Python 参照の 256 要素すべて一致 | ACCX の合算方向・飽和・レーン対応の誤解が露見 |
| `ex03` fir16 | 16tap Q15 FIR と、**128bit アクセスの丸め**（窓を16バイト境界に載せる必要があること） | カーネルは境界に揃えた窓を受け取る。丸めプローブは「x+2 のロードが x のバイトを返す」こと、`EE.SRC.Q` プローブはコピー不要の窓の作り方 | 境界ルールと、`EE.SRC.Q` のオペランド順が決まる |
| `ex04` qr | `LD.QR` / `ST.QR` / `MV.QR` の往復と、**`LD.QR` のインターロック** | Table 1.7-2 に無い命令。実測で確定した段（LD.QR の def は M）から、距離 0 で 1 サイクル、距離 1 以上で 0 | 実測段の裏付けが装置上で取れる（計測ファームの結論の追試） |
| `ex05` saturation | ACCX は 40bit で**飽和**する、`EE.SRS.ACCX` も 32bit で飽和する | 32000×32000 を 200 回。数学的和 1.6e12 に対し 40bit 上限 2^39-1 で頭打ち | 飽和の丸め位置（積ごと / 和ごと）と範囲の確定 |
| `ex06` fft | `EE.FFT.R2BF.S16`（レーン並列バタフライ、sel2 で並び替え）と `EE.CMUL.S16`（(re,im) ペアの複素乗算、SAR シフト） | 疑似コードの op_a/op_b 構成を Python でそのまま実装して一致を要求 | レーンの対応（sel2 / sel4 の意味）が確定する |

## ログの書式

```
EX <例番号> <名前> BEGIN
EX ex01 encoding DATA step_ld_accx+16=16          # 実測値（キー=値）
EX ex01 encoding DATA raw_field1_ld=8
EX ex02 matmul16 DATA a=<256 個の16進>             # 入力も全部出す（Python が再計算できるように）
EX ex05 saturation CHECK accx_40bit_clamp pie=X ref=Y ok|FAIL
EX ex02 matmul16 RESULT ok=1 fail=0
END
SUMMARY checks_ok=N checks_fail=0
```

`DATA` は実測・入出力、`CHECK` はファーム内 C 参照との比較、`RESULT` は例ごとの自己採点。ホスト側の
チェッカーは `DATA` から入力と結果を取り出し、Python で三度目の計算をして `FAIL` を出します。
`FAIL` が出た例では、`DATA first_mismatch ...`（先頭 3 件）で最初に食い違った位置が分かります。

## 結果（第1回ラン: `pie-examples-20260914T174138Z.log`）

ファーム内の自己採点で 21 ok / 85 fail、ホスト側チェッカーで 17/22。**失敗はすべてこちら側の誤り**で、
装置は文書どおりに動いていました。確定したこと（機械可読版は `data/pie_examples_measured.json`）:

| 知見 | 実機が言ったこと |
|---|---|
| アドレス後置インクリメント | `field=1` で `EE.LD.ACCX.IP` も `EE.ST.ACCX.IP` も **8 バイト**進む → アセンブラの刻みが正しく、`EE.ST.ACCX.IP` の構文行（4刻み）と Operation（1刻み）はどちらもマニュアル側の欠陥 |
| `EE.SRS.ACCX` | `sat32(ACCX >> as[5:0])` を3ケースで確認（0 と 5 のシフトは 32bit 飽和、8 は厳密） |
| `EE.VMULAS.S16.ACCX` | 16×16 行列積 **256/256 一致**＝8レーンの積が ACCX に合算される（疑似コードどおり） |
| ACCX | 40bit で飽和。数学的和 1.6384e12 に対し **549755813887 = 2^39-1** で頭打ち、`>>9` で厳密、`>>0` で int32 飽和 |
| `EE.FFT.R2BF.S16` | **オペランド列は MSB 先**（左のフィールドが上位レーン）。この読みで sel2=0/1 の両方が実機のレーンと完全一致し、逆の読みは反証された |
| `EE.CMUL.S16` | sel4=0/1 × SAR=0/12 の4ケースすべて一致（複素乗算＋算術シフト） |
| `EE.VLD.128.IP` の丸め | 2バイトずつ滑らせた窓は**8サンプル連続で同じ16バイト**を読む。実機の49出力すべてが「窓を16バイト境界に切り下げる」モデルで説明できた（＝TRM p49 の丸めが原因。カーネル側の誤り） |
| `LD.QR` インターロック | 距離 0 で 1.000 サイクル、距離 1 で 0.000、距離 2 で 0.042（ノイズ内）＝実測段の追試成功 |
| `EE.BITREV` | 4ワードの生データのみ（`FFT_BIT_WIDTH` を設定する命令が見当たらず期待値が作れないので「解釈保留」として保存） |

## 次の一手

1. 修正済みの ex03（境界に揃えた窓＋丸め/ファネルのプローブ）を実機で流す → `EE.SRC.Q` のオペランド順と、
   コピー不要で窓を作れるかの決着。
2. その結果で `EE.FFT.*` の多段（8点 → 32点）を書く。sel2=0/1 のレーン並びは確定済みなので、
   段ごとに中間バッファを出して1段ずつ検証できる。
3. `EE.SRCMB.S16.QACC` / `EE.ST.QACC_*.IP` の詰め方（8並列 QACC 経路）は未着手。

## 信頼度（正直なところ）

- `ex01`–`ex05`: 予測はすべて**確定した知識**（符号化は `tools/asm_toolchain.py` がアセンブラとビット単位で
  照合済み、意味は TRM の Operation 疑似コード）から出しています。外れたらそれは知識の欠陥で、そのまま
  成果物になります。
- `ex06`: 単命令プリミティブ（R2BF の sel2=0/1、CMUL の sel4=0/1）は第1回ランで実機と一致しました。
  多段 FFT はその上に積む段で、まだ書いていません（並びが確定したので次の一手）。
- 全例に言えること: PIE の 128bit アクセスはアドレス下位 4bit を 0 に丸める（TRM p49 の figure 1.7-1）ので、
  **アラインメント違反は例外にならず、静かに隣のバイトを読み書きします**。ex03 はこれを実機で再現し、
  そのうえでカーネルを境界に揃えた窓を受け取る形に直してあります。

## 再現性

- `DATA a=` / `DATA bt=` / `DATA x=` が入力そのものなので、ログ 1 ファイルだけで検算が完結します。
- 乱数はリニア (LCG, seed は ENV 行) なので、同じ firmware revision なら同じ入力になります。
- チェッカー自身の検査は `tools/selftest_examples_checker.py`（参照実装から作ったログが通ること、
  1 フィールドずつ壊したときに必ず落ちること）。
