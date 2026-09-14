# ストール段数のモデル（TRM 1.7.1 の読み方）

`analyze_sequence` が使っている規則と、その根拠、そして**マニュアル自身の矛盾**の扱いを記録する。

## 一次情報（TRM v1.8 p65-66, 74-75）

- Table 1.7-1（p65）: 5段パイプラインの段番号は **I = "—", R = 0, E = 1, M = 2, W = 3**。
- 1.7.1（p65）の規則:

  > If instruction A prepares the result to be written to register X at the end of the SA pipeline stage,
  > and instruction B reads the data in register X at the beginning of the SB pipeline stage, then
  > instruction A must be issued D = max(SA - SB + 1, 0) cycles before instruction B.

  つまり **D は「最小発行間隔（サイクル）」**で、インターロック（＝実際に止まる段数）は **D - 1 = max(SA - SB, 0)**。
- 同じ p65 に bypass（forwarding）があることが明記されている。この規則はすでに forwarding を織り込んだ形。
- Table 1.7-2（p66-74）が全拡張命令のオペランド別 use/def 段を与える。

## 発見した矛盾（一次情報側の問題）

p65 の計算例は `SA = W, SB = E` として **D = max(2-1+1, 0) = 2** と書いている。すなわち例は **W を 2** として
扱っているが、Table 1.7-1 は **W = 3** と定義している。両者は両立しない（例の側の書き間違いと見られる）。

**この矛盾が解析に到達しない理由**: Table 1.7-2 のセルは use/def ともに **1（E）と 2（M）しか使わず、
W（3）は一度も現れない**。したがって表から駆動する限り「W は 2 か 3 か」を決める必要がない。

→ 実装は `D = max(SA - SB + 1, 0)`、`stall = D - 1 = max(SA - SB, 0)`、SA/SB ∈ {1, 2} とした。
   `max(SA-SB, 0)` は {0, 1} の2値しか取らない。

## 表から導かれる依存の全体像（実測）

- def 段が 2（M）で、use 段が 1（E）になりうるレジスタ = **ACCX, QACC_H, QACC_L, UA_STATE, as0, qs** の6つ。
  この6つだけが「1ストール」を生みうる。他は def/use が同じ段か、def ≤ use。
- 例: `EE.LD.ACCX.IP`（ACCX を M で書く）→ `EE.SRS.ACCX`（ACCX を E で読む）= 1ストール。
  `EE.VRELU.S16`（qs を M で書く）→ `EE.MOV.S16.QACC`（qs を E で読む）= 1ストール。
- E→E（`EE.ANDQ` → `EE.ANDQ` など）は 0。

## モデル化していないもの（ツールはそう明示して返す）

1. **1.7.2 ハードウェア資源ハザード（p74）**: 16bit乗算器は8個。同時使用は1命令のみ通り、残りは遅延
   （本文の例では1サイクル）。命令ごとの資源占有数が表に無いため**計算しない**。
2. **1.7.3 制御ハザード（p74-75）**: 分岐成立で R/E 段が破棄され2サイクル停止。PIE に分岐は無く、
   ネイティブ Xtensa の分岐は TRM が時間を定義していないため対象外。
3. **基本ISAの命令**: TRM は Xtensa 基本ISAのタイミングを定義していない（そもそも公式PDFが無い）。
4. **副レジスタの別名**: `qz1` / `fu0`〜 等は表記どおり別オペランドとして厳密一致で判定する。
   別名関係（`qz1` が `qz` の上位半分か等）は抽出データでは確定できないため主張しない。
5. **依存鎖の重畳**: 隣接ペアごとの独立加算という一次近似。長い依存鎖や資源競合が重なる場合は実機で確認する。
