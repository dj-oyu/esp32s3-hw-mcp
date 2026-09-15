# 11 — レポートの連続 printf がタスク WDT を踏み、その間に1行消える

`examples/` のファームは 1 回の起動で ~75 KB のレポートを `printf` で吐く。2026-09-15 の実機ランで、
**`EX ex10 motion DATA sad_total=43480` の行だけが丸ごとログに届かず**、ホスト側チェッカーが ex10 を
`missing ['sad_total']` で落とした。同じ位置で 2 回再現し、原因は「レポートを吐いている間ずっと
IDLE0 に一度も制御が渡らない」ことだった。このノートは観測した事実と、そこから言える規則を分けて書く。

## 1. 症状（実機のログそのまま）

2 ラン連続で、`sad_blocks=64` の直後に WDT の報告が割り込み、その次は `sad_plain_textbook=43480` から
再開する。`sad_total` の行（`main.c:1352` の printf）は**バイト列として存在しない**:

```
EX ex10 motion DATA sad_accx=0000a9d8,00000000
EX ex10 motion DATA sad_blocks=64
E (6748) task_wdt: Task watchdog got triggered. The following tasks/users did not reset the watchdog in time:
E (6748) task_wdt:  - IDLE0 (CPU 0)
E (6748) task_wdt: Tasks currently running:
E (6748) task_wdt: CPU 0: main
E (6748) task_wdt: CPU 1: IDLE1
E (6748) task_wdt: Print CPU 0 (current core) backtrace
Backtrace: 0x420109EE:0x3FC91F90 ... 0x4200AE06:0x3FC9C460 ...
EX ex10 motion DATA sad_plain_textbook=43480        ← sad_total が無いまま次へ
```

2 ランのバイト位置は内容として完全に同一（差分は app version 文字列の 6 バイトだけ）。つまり
タイミングの偶然ではなく、決定的に同じ場所で起きている。`SUMMARY checks_ok=28 checks_fail=1`（ファーム側）
/ ホスト側 `59 of 63`、`62 of 63`。

## 2. 機序（バックトレースから確定した部分）

`Backtrace` を `xtensa-esp32s3-elf-addr2line -f -e build_examples/pie_examples.elf` で引くと、
main タスクはこの時こうなっていた:

```
0x4200AE06  ex10                    main.c:1352        ← まさに sad_total の printf
0x420118DF  printf                  printf.c:41
0x42011BC9  vfprintf                vfprintf.c:581
0x420116CE  __bufio_put             bufio.c:187
0x42013489  bufio_write             stdio_private.h:183
0x42001B74  console_write           stdio_vfs.c:84
0x42006E3A  write                   syscalls.c:51
0x420088E5  esp_vfs_write           vfs_calls.c:77
0x42007B12  uart_write              esp_driver_uart/src/uart_vfs.c:245
0x420073A2  uart_ll_get_txfifo_len  esp_hal_uart/esp32s3/include/hal/uart_ll.h:434
```

そして WDT 側の最内枠は `task_wdt_isr` → `task_wdt_timeout_handling`（IDF 6.0 は**割り込みから**
`ESP_EARLY_LOGE` で報告する）。

決定的なのは `uart_write` が呼んでいる 1 バイト書き込みの中身:

```c
static void uart_tx_char(int fd, int c)
{
    uart_dev_t* uart = s_ctx[fd]->uart;
    const uint8_t ch = (uint8_t) c;

    while (uart_ll_get_txfifo_len(uart) < 2) {
        ;                      // ← 空ループ。yield も vTaskDelay も待ち時間も無い
    }

    uart_ll_write_txfifo(uart, &ch, 1);
}
```

UART は遅い側（115200 baud = 11.5 KB/s）で、CPU は FIFO（128 バイト）を一瞬で埋められる。つまり
レポートを吐いている間 main はこの空ループを回り続け、**IDLE0 に制御が渡らない**。このファームの
レポートは ~75 KB ≈ 6.5 秒ぶんあり、タスク WDT の IDLE0 監視（5 秒）を1回の連続スピンで超える。
`BENCH` の数字が ccount 差分で取れているのと同じ理由で、レポートの遅さ自体は故障ではない。

## 3. 直し方（`report_tick()`）

`main.c` の値を吐く 5 か所（`print_i16` / `print_i32` のループと、インライン展開されている 3 か所）に
「32 値ごとに `vTaskDelay(1)`」を入れた:

```c
#define REPORT_YIELD_EVERY 32
static unsigned s_report_emitted;

static void report_tick(void)
{
    if (++s_report_emitted >= REPORT_YIELD_EVERY) {
        s_report_emitted = 0;
        vTaskDelay(1);         /* IDLE0 に渡してタスク WDT の IDLE0 監視をリセットする */
    }
}
```

- `vTaskDelay(0)` では効かない（自分と同優先度以上にしか譲らず、IDLE0 は優先度 0）。1 tick 必要。
- 1 チャンク = 32 値 ≈ 160 バイト ≈ 14 ms なので、1 回のスピンが 5 秒に届くことはなくなる。
  追加の実時間は「出力チャンク数 × 10 ms」程度で、`BENCH`（カーネル周辺の ccount 差分）には無関係。

再走の結果（`/workspace/backups/pie-examples-20260915T032511Z.log`）:

- `task_wdt` の出現回数 **0**（直前の 2 ランは 6 行ずつ）
- `sad_total=43480` が復帰し、ホスト側チェッカーは **73/73 PASS**、ファーム側 `SUMMARY checks_ok=29 checks_fail=0`

## 4. 規則として残すこと

1. **数百バイト以上のレポートを吐くファームは、値を吐くループで定期的に yield する。** IDF のコンソール
   書き込みは FIFO を空ループで待つ実装なので、「印刷が長い = IDLE0 が餓死する」が直結する。
2. **WDT やパニックの報告は同じ UART を ISR から奪う。** その瞬間に送信中だった行は、ホストに届かない
   ことがある（この例では行まるごと 1 本）。ログの「行数」を検査するツールは、欠けた行を**静かなデータ
   欠落**として扱うので、まず `task_wdt` を grep して切り分ける。
3. 計測ファームのレポートは「時間のかかる作業」なので、`REPORT_YIELD_EVERY` のような定数を最初から
   用意しておくと、例を足してレポートが伸びても同じ罠を踏まない。

## 5. 断定していないこと

- 「WDT の報告が、送信中だった `sad_total` のバイト列を**どうやって**捨てたか」の正確な機序は特定して
  いない。観測できているのは (a) その瞬間 main が `main.c:1352` の console 書き込みの中にいたこと、
  (b) その行がバイト列としてログに存在しないこと、(c) 同じ位置で 2 回再現したこと、(d) yield を
  入れたら WDT ごと消えて行も戻ったこと。機序の説明（ISR 側の報告が同じ TX 経路を奪う）は推論。
- `uart_write` の 1 バイト書き込み自体は**ブロックするだけで欠落させない**（`for` ループで全バイトを
  書く実装）ことをソースで確認している。つまり欠落は「アプリが書かなかった」のではなく、
  WDT 報告と衝突した側で起きている。
- ホスト側（capture_serial.py / WSL のシリアル）由来の欠落ではない: 2 ランで内容位置が完全に一致し、
  capture の stdout をパイプからファイルに変えても同じ位置で再現し、ファーム側の修正だけで消えた。
