# -*- coding: utf-8 -*-
"""
AXTI 盘口微观结构实时监控 (TWS API 版)
依赖: pip install ib_async
运行: python axti_tws_monitor.py
前提: TWS已登录且开启API (Global Configuration -> API -> Settings)
"""
from ib_async import IB, Stock, Ticker
from collections import deque
from datetime import datetime
import math
import sys

# ================= 配置 =================
SYMBOL = "AXTI"
EXCHANGE = "SMART"
CURRENCY = "USD"
TWS_HOST = "127.0.0.1"
TWS_PORT = 7497          # 实盘=7496, 模拟盘=7497, 按你的TWS设置改
CLIENT_ID = 21           # 任意不冲突的整数

BASELINE_LEN = 120       # 滚动基准窗口(采样点数)
MIN_BASELINE = 20        # 至少多少个点后才开始报警
SPREAD_Z_TH = 2.0        # spread z-score 报警阈值
IMB_FLIP_TH = 0.4        # 失衡翻转阈值
OFI_WINDOW = 60          # OFI累计窗口(秒)
PRINT_EVERY = 5          # 每几秒打印一次状态行
# ========================================


class Monitor:
    def __init__(self):
        self.spreads = deque(maxlen=BASELINE_LEN)
        self.imbs = deque(maxlen=BASELINE_LEN)
        self.session_low = math.inf
        self.ofi_events = deque()      # (timestamp, signed_size)
        self.prev_bid = None
        self.prev_ask = None
        self.prev_bid_size = None
        self.prev_ask_size = None
        self.last_print = 0
        self.alerts = []

    # ---------- 工具 ----------
    @staticmethod
    def z(hist, x):
        if len(hist) < MIN_BASELINE:
            return None
        m = sum(hist) / len(hist)
        var = sum((v - m) ** 2 for v in hist) / len(hist)
        sd = math.sqrt(var)
        return (x - m) / sd if sd > 1e-12 else 0.0

    def alert(self, msg):
        line = f"[{datetime.now():%H:%M:%S}] ⚠ {msg}"
        self.alerts.append(line)
        print("\a" + line)          # \a = 终端蜂鸣
        try:                        # Windows弹窗(可选, 失败静默)
            import ctypes
            ctypes.windll.user32.MessageBeep(0x30)
        except Exception:
            pass

    # ---------- 核心: 每次tick更新 ----------
    def on_ticker(self, ticker: Ticker):
        bid, ask = ticker.bid, ticker.ask
        bs, asz = ticker.bidSize, ticker.askSize
        last = ticker.last
        now = datetime.now().timestamp()

        if not (bid and ask and bid > 0 and ask > bid):
            return
        # ib_async 刚订阅时 size 可能是 NaN, 归零避免污染 imb/OFI
        bs = bs if bs == bs else 0.0
        asz = asz if asz == asz else 0.0

        mid = (bid + ask) / 2
        spread_bps = (ask - bid) / mid * 1e4
        imb = (bs - asz) / (bs + asz) if (bs or asz) else 0.0
        micro = (bid * asz + ask * bs) / (bs + asz) if (bs + asz) else mid

        # --- OFI (Cont et al. 2014 的L1版本) ---
        ofi_delta = 0.0
        if self.prev_bid is not None:
            if bid > self.prev_bid:
                ofi_delta += bs
            elif bid == self.prev_bid:
                ofi_delta += bs - self.prev_bid_size
            else:
                ofi_delta -= self.prev_bid_size
            if ask < self.prev_ask:
                ofi_delta -= asz
            elif ask == self.prev_ask:
                ofi_delta -= asz - self.prev_ask_size
            else:
                ofi_delta += self.prev_ask_size
        self.prev_bid, self.prev_ask = bid, ask
        self.prev_bid_size, self.prev_ask_size = bs, asz
        self.ofi_events.append((now, ofi_delta))
        while self.ofi_events and now - self.ofi_events[0][0] > OFI_WINDOW:
            self.ofi_events.popleft()
        ofi = sum(v for _, v in self.ofi_events)

        # --- 报警判断 (先用旧基准算z, 再入队) ---
        spread_z = self.z(self.spreads, spread_bps)
        if spread_z is not None and spread_z > SPREAD_Z_TH:
            self.alert(f"Spread异常变宽 z={spread_z:.1f} ({spread_bps:.1f}bps) — 做市商防御信号")

        if len(self.imbs) >= 5:
            recent = list(self.imbs)[-5:]
            avg_prev = sum(recent) / len(recent)
            if avg_prev > IMB_FLIP_TH and imb < -IMB_FLIP_TH:
                self.alert(f"盘口失衡翻转 买堆积→卖堆积 ({avg_prev:+.2f} → {imb:+.2f})")
            elif avg_prev < -IMB_FLIP_TH and imb > IMB_FLIP_TH:
                self.alert(f"盘口失衡翻转 卖堆积→买堆积 ({avg_prev:+.2f} → {imb:+.2f})")

        if last and last < self.session_low - 1e-9 and self.session_low != math.inf:
            self.alert(f"创监控时段新低 {last:.2f}")
        if last:
            self.session_low = min(self.session_low, last)

        self.spreads.append(spread_bps)
        self.imbs.append(imb)

        # --- 状态行 ---
        if now - self.last_print >= PRINT_EVERY:
            self.last_print = now
            zs = f"{spread_z:+.1f}" if spread_z is not None else " --"
            print(f"{datetime.now():%H:%M:%S}  last={last or mid:.2f}  "
                  f"bid={bid:.2f}x{bs:.0f}  ask={ask:.2f}x{asz:.0f}  "
                  f"spr={spread_bps:.1f}bps(z{zs})  imb={imb:+.2f}  "
                  f"micro={micro:.3f}  OFI({OFI_WINDOW}s)={ofi:+.0f}")


def main():
    ib = IB()
    print(f"连接TWS {TWS_HOST}:{TWS_PORT} ...")
    try:
        ib.connect(TWS_HOST, TWS_PORT, clientId=CLIENT_ID, timeout=10)
    except Exception as e:
        print(f"连接失败: {e}\n检查: TWS是否登录? API是否开启? 端口号对不对(实盘7496/模拟7497)?")
        sys.exit(1)

    contract = Stock(SYMBOL, EXCHANGE, CURRENCY)
    ib.qualifyContracts(contract)
    print(f"已定位合约: {contract.symbol} conId={contract.conId}")

    # 没有实时订阅时自动退回延迟数据(类型3); 有订阅则用实时(类型1)
    ib.reqMarketDataType(1)
    ticker = ib.reqMktData(contract, "", False, False)

    mon = Monitor()
    ticker.updateEvent += mon.on_ticker

    print(f"开始监控 {SYMBOL} — Ctrl+C 停止\n"
          f"报警条件: spread z>{SPREAD_Z_TH} | 失衡翻转±{IMB_FLIP_TH} | 时段新低\n")
    try:
        ib.run()
    except KeyboardInterrupt:
        print(f"\n停止。共触发 {len(mon.alerts)} 条报警:")
        for a in mon.alerts:
            print(" ", a)
        ib.disconnect()


if __name__ == "__main__":
    main()
