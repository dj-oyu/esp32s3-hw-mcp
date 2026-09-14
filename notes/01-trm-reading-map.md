# TRM v1.8 の読み方（どこに何があるか）

一次情報: `sources/esp32-s3_technical_reference_manual_en.pdf`（TRM v1.8、1531ページ、印字ページ＝PDFページ）。
ブックマーク1150件。`tools/pdf_corpus.py toc|find|text|grep --corpus corpus/trm-s3` で辿れる。

## 性能チューニングに関係する章

| 章 | ページ | 内容 | 使いどころ |
|---|---|---|---|
| 1 PIE | p39-303 | 拡張命令一式 | 1.6 命令一覧（分類別）、**1.7 命令性能 p65-75**、1.8 命令個別仕様 p76-303 |
| 4 System and Memory | p400-409 | アドレスマップ、内部/外部メモリ、GDMAアドレス空間 | メモリ配置の一次情報 |
| 7 Reset and Clock | p526-533 | リセット源、クロック | 周波数・クロック源 |
| 15 Permission Control (PMS) | p683-800 | 権限（読み書き実行）と分割線 | 保護境界、PMS粒度 |
| 17 System Registers | p822-841 | CPU制御系 | |
| 10 Low-power Management | p565-633 | 電力モード | 省電力と性能のトレードオフ |

**キャッシュ専用の章は TRM v1.8 に無い。** 命令/データキャッシュの詳細は p403（4.3.3 External Memory）
周辺と ESP-IDF 側の記述が主。→ キャッシュ関連の主張をするときは出所を明示すること。

## 1.7 Instruction Performance（本プロジェクトの中核）

- 1.7 冒頭（p65）: Xtensa は **5段パイプライン I/R/E/M/W**（Table 1.7-1: I=命令フェッチ, R=デコード,
  E=実行, M=メモリアクセス, W=ライトバック。段番号 0=R, 1=E, 2=M, 3=W）。
- 1.7.1 Data Hazard（p65-73）: インターロック条件 **D = max(SA - SB + 1, 0)**。
  Table 1.7-2（p66-74）が全拡張命令のオペランド／特殊レジスタの use/def 段を与える。
- 1.7.2 Hardware Resource Hazard（p74）: 同一ハードウェア資源の同時使用は1命令のみ通し、残りを遅延
  （例: 16bit乗算器は8個。E段で4個＋M段で8個を要求すると1サイクル遅延）。
- 1.7.3 Control Hazard（p74-75）: 分岐ペナルティは2サイクル（R/E段の命令が破棄される）。

## 1.8 Extended Instruction Functional Description（p76-303、220命令）

1命令1節、アルファベット順。各節の構成は一定:
`Instruction Word`（ビット列）/ `Assembler Syntax` / `Description` / `Operation`（擬似コード、行番号付き）。
→ 構造が完全に規則的なので機械可読化できる。例外は `EE.VMULAS.S8.QACC.LD.IP`（p228、Description節なし）。

- 1.5.1 Registers（p45-48, Table 1.5-1）: レジスタ語彙の一次情報
  （AR 16本/FR 16本/QR 8本 128bit/SAR/SAR_BYTE/ACCX/QACC_H/QACC_L/FFT_BIT_WIDTH/UA_STATE）。
  添字付きオペランド（`fu0`〜`fu3`, `as0`/`as1` 等）の説明は p45。
- 1.5.3 Data Format and Alignment（p48-49）: 16byteアライン強制の話。
- 1.5.4 Data Overflow and Saturation（p49）: 飽和の扱い。

## 落とし穴

- **Table 1.7-2 のセルは PDF テキスト層で空白が落ちる**（`qv2,as01,as1,` = "qv 2, as0 1, as 1"）。
  行の高さは 15.9pt、セル内の折返しは 7.8pt。セルはアンカー（命令名列）に対して垂直方向に中央寄せで、
  3行になるとアンカーの15.6pt下まで伸びるため、**「最寄りアンカー」ではなく行境界で割り当てる**必要がある。
- 表は **p66 のヘッダ行が p67 以降には繰り返されない**（p66だけにヘッダがある）。また最終行は p74 の
  先頭2行（`EE.ZERO.Q`, `EE.ZERO.QACC`）まで続き、その下に 1.7.2 の本文が来る。範囲を1ページ間違えると
  行が丸ごと消える（実際 p73 で切って 215 行になっていたのを 217 行に修正）。
- `EE.BITREV` の表（`ax`）と 1.8 の構文（`as`）が食い違う。pypdf と PyMuPDF の両方で同じなので
  抽出ミスではない。一次情報の側の不一致として扱い、`data/pie_review.json` に残す。
