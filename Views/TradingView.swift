import Charts
import SwiftUI

/// The Trading window (⌘⇧T): backtests and paper accounts from Jugnu's
/// simulator. Everything shown here is simulation -- there's no brokerage
/// anywhere in it, and no tool in Jugnu can place a real order.
///
/// The strategy is always drawn against buy & hold, and the warnings sit
/// under the headline number rather than in a corner, because a backtest
/// that only shows its best figure is the most common way to lose money.
struct TradingView: View {
    @ObservedObject var appState: AppState
    @State private var tab: String

    init(appState: AppState, initialTab: String = "backtest") {
        self.appState = appState
        _tab = State(initialValue: initialTab)
    }

    @State private var symbol = "AAPL"
    @State private var strategy = "sma"
    @State private var params = ""
    @State private var from = "2015-01-01"
    @State private var slippage = 5.0
    @State private var cash = 10_000.0
    @State private var showTrades = false

    private var payload: [String: JSONValue] { appState.trading.object }
    private var action: String { payload["action"]?.string ?? "" }
    private var failed: String? {
        payload["ok"]?.bool == false ? (payload["error"]?.string ?? "That didn't work.") : nil
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if tab == "backtest" { backtestTab } else { paperTab }
                }
                .padding(14)
            }
        }
        .background(Theme.windowBackground)
        .onAppear {
            if appState.trading.isNull { appState.runTrading("strategies") }
        }
    }

    // MARK: Header

    private var header: some View {
        HStack(spacing: 10) {
            FireflyView(state: appState.tradingBusy ? .thinking : .idle, size: 15)
            Text("Trading").font(Theme.text(12.5, weight: .semibold)).foregroundStyle(Theme.ink)
            Picker("", selection: $tab) {
                Text("Backtest").tag("backtest")
                Text("Paper").tag("paper")
            }
            .pickerStyle(.segmented).labelsHidden().fixedSize()
            Spacer()
            Text("simulation only · no broker, no real money")
                .font(Theme.text(10.5)).foregroundStyle(Theme.inkFaint)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(Theme.panel)
        .overlay(alignment: .bottom) { Rectangle().fill(Theme.border).frame(height: 1) }
        .zIndex(1)
    }

    // MARK: Backtest

    private var backtestTab: some View {
        VStack(alignment: .leading, spacing: 16) {
            controls
            if let failed {
                Label(failed, systemImage: "exclamationmark.circle")
                    .font(Theme.text(12)).foregroundStyle(Theme.clay)
            }
            if action == "backtest", payload["ok"]?.bool == true {
                results
            } else if action == "sweep", payload["ok"]?.bool == true {
                sweepResults
            } else if !appState.tradingBusy {
                Text("Pick a symbol and a strategy, then Run.\nEvery result is compared with buying and holding the same thing.")
                    .font(Theme.text(12)).foregroundStyle(Theme.inkFaint)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var controls: some View {
        VStack(spacing: 8) {
            HStack(spacing: 8) {
                labelled("Symbol", width: 110) {
                    TextField("AAPL", text: $symbol).textFieldStyle(.roundedBorder)
                }
                labelled("Strategy", width: 130) {
                    Picker("", selection: $strategy) {
                        ForEach(strategyNames, id: \.self) { Text($0).tag($0) }
                    }
                    .labelsHidden()
                }
                labelled("From", width: 110) {
                    TextField("2015-01-01", text: $from).textFieldStyle(.roundedBorder)
                }
                labelled("Params", width: 130) {
                    TextField("fast=20 slow=100", text: $params).textFieldStyle(.roundedBorder)
                }
            }
            HStack(spacing: 8) {
                labelled("Slippage", width: 120) {
                    HStack(spacing: 4) {
                        Slider(value: $slippage, in: 0...50, step: 1)
                        Text("\(Int(slippage))bps").font(Theme.mono(11)).foregroundStyle(Theme.inkDim)
                            .frame(width: 46, alignment: .trailing)
                    }
                }
                labelled("Start cash", width: 110) {
                    TextField("10000", value: $cash, format: .number).textFieldStyle(.roundedBorder)
                }
                Spacer()
                Button("Sweep parameters") { appState.runTrading("sweep", request) }
                    .buttonStyle(JugnuButtonStyle(kind: .secondary, compact: true))
                    .disabled(appState.tradingBusy)
                Button(appState.tradingBusy ? "Running…" : "Run") { appState.runTrading("backtest", request) }
                    .buttonStyle(JugnuButtonStyle(kind: .primary, compact: true))
                    .disabled(appState.tradingBusy)
                    .keyboardShortcut(.return, modifiers: [])
            }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 10).fill(Theme.panel))
    }

    private var request: [String: JSONValue] {
        ["symbol": .string(symbol), "strategy": .string(strategy), "params": .string(params),
         "start": .string(from), "slippage_bps": .number(slippage), "cash": .number(cash)]
    }

    private var strategyNames: [String] {
        let listed = (payload["strategies"]?.array ?? []).compactMap { $0["name"]?.string }
        return listed.isEmpty ? ["sma", "rsi", "breakout", "volatility", "hold"] : listed
    }

    @ViewBuilder
    private var results: some View {
        let metrics = payload["metrics"]?.object ?? [:]
        let benchmark = payload["benchmark_metrics"]?.object ?? [:]
        let mine = metrics["total_return"]?.double ?? 0
        let theirs = benchmark["total_return"]?.double ?? 0

        VStack(alignment: .leading, spacing: 4) {
            Text("\(payload["symbol"]?.string ?? "") · \(payload["strategy"]?.string ?? "")")
                .font(Theme.text(13, weight: .semibold)).foregroundStyle(Theme.ink)
            Text("\(payload["from"]?.string ?? "") → \(payload["to"]?.string ?? "")"
                 + " · \(Int(payload["slippage_bps"]?.double ?? 0))bps slippage")
                .font(Theme.text(11)).foregroundStyle(Theme.inkFaint)
            HStack(alignment: .firstTextBaseline, spacing: 14) {
                figure("strategy", mine, big: true, good: mine > theirs)
                figure("buy & hold", theirs, big: true, good: theirs >= mine)
            }
            .padding(.top, 4)
        }

        ForEach(payload["warnings"]?.array.compactMap(\.string) ?? [], id: \.self) { note in
            Label(note, systemImage: "exclamationmark.triangle.fill")
                .font(Theme.text(11.5)).foregroundStyle(Theme.clay)
        }

        chart
        statsTable
        costCurve
        monteCarlo
        tradesList
    }

    private func figure(_ label: String, _ value: Double, big: Bool = false, good: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(label).font(Theme.text(10.5)).foregroundStyle(Theme.inkFaint)
            Text(percent(value))
                .font(Theme.text(big ? 20 : 13, weight: .semibold))
                .foregroundStyle(good ? Theme.success : Theme.ink)
        }
    }

    private var chart: some View {
        let equity = (payload["equity"]?.array ?? []).compactMap(\.double)
        let benchmark = (payload["benchmark"]?.array ?? []).compactMap(\.double)
        let dates = (payload["dates"]?.array ?? []).compactMap(\.string)
        return Chart {
            ForEach(Array(equity.enumerated()), id: \.offset) { index, value in
                LineMark(x: .value("i", index), y: .value("equity", value),
                         series: .value("series", "strategy"))
                    .foregroundStyle(Theme.clay)
            }
            ForEach(Array(benchmark.enumerated()), id: \.offset) { index, value in
                LineMark(x: .value("i", index), y: .value("equity", value),
                         series: .value("series", "buy & hold"))
                    .foregroundStyle(Theme.inkFaint)
                    .lineStyle(StrokeStyle(lineWidth: 1, dash: [3, 3]))
            }
        }
        .chartXAxis {
            AxisMarks(values: [0, max(0, equity.count - 1)]) { mark in
                AxisValueLabel {
                    if let index = mark.as(Int.self), dates.indices.contains(index) {
                        Text(dates[index]).font(Theme.text(9.5))
                    }
                }
            }
        }
        .chartYAxis {
            AxisMarks(position: .leading) { value in
                AxisGridLine().foregroundStyle(Theme.border)
                AxisValueLabel {
                    if let amount = value.as(Double.self) {
                        Text("$\(Int(amount / 1000))k").font(Theme.text(9.5))
                    }
                }
            }
        }
        .frame(height: 190)
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 10).fill(Theme.panel))
        .overlay(alignment: .topTrailing) {
            HStack(spacing: 8) {
                legend("strategy", Theme.clay)
                legend("buy & hold", Theme.inkFaint)
            }
            .padding(12)
        }
    }

    private func legend(_ text: String, _ colour: Color) -> some View {
        HStack(spacing: 3) {
            RoundedRectangle(cornerRadius: 1).fill(colour).frame(width: 10, height: 2)
            Text(text).font(Theme.text(10)).foregroundStyle(Theme.inkFaint)
        }
    }

    private var statsTable: some View {
        VStack(spacing: 0) {
            HStack {
                Text("").frame(maxWidth: .infinity, alignment: .leading)
                Text("strategy").frame(width: 90, alignment: .trailing)
                Text("buy & hold").frame(width: 90, alignment: .trailing)
            }
            .font(Theme.text(10.5)).foregroundStyle(Theme.inkFaint)
            .padding(.bottom, 4)
            ForEach(payload["rows"]?.array ?? [], id: \.compactText) { row in
                HStack {
                    Text(row["label"]?.string ?? "").frame(maxWidth: .infinity, alignment: .leading)
                    Text(row["strategy"]?.string ?? "").frame(width: 90, alignment: .trailing)
                        .foregroundStyle(Theme.ink)
                    Text(row["benchmark"]?.string ?? "").frame(width: 90, alignment: .trailing)
                        .foregroundStyle(Theme.inkDim)
                }
                .font(Theme.mono(11))
                .padding(.vertical, 2)
            }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 10).fill(Theme.panel))
    }

    private var costCurve: some View {
        let curve = payload["cost_curve"]?.object ?? [:]
        return Group {
            if !curve.isEmpty {
                VStack(alignment: .leading, spacing: 5) {
                    Text("Return at other slippage levels")
                        .font(Theme.text(11, weight: .semibold)).foregroundStyle(Theme.inkFaint)
                    HStack(spacing: 14) {
                        ForEach(curve.keys.sorted { (Double($0) ?? 0) < (Double($1) ?? 0) }, id: \.self) { key in
                            VStack(spacing: 1) {
                                Text("\(key)bps").font(Theme.text(10)).foregroundStyle(Theme.inkFaint)
                                Text(percent(curve[key]?.double ?? 0))
                                    .font(Theme.mono(11.5))
                                    .foregroundStyle((curve[key]?.double ?? 0) > 0 ? Theme.ink : Theme.danger)
                            }
                        }
                    }
                }
                .padding(10)
                .background(RoundedRectangle(cornerRadius: 10).fill(Theme.panel))
            }
        }
    }

    private var monteCarlo: some View {
        let carlo = payload["monte_carlo"]?.object ?? [:]
        return Group {
            if carlo["enough_trades"]?.bool == true {
                VStack(alignment: .leading, spacing: 4) {
                    Text("If these \(carlo["round_trips"]?.int ?? 0) trades had happened in a different order")
                        .font(Theme.text(11, weight: .semibold)).foregroundStyle(Theme.inkFaint)
                    HStack(spacing: 16) {
                        figure("worst 5%", carlo["p05"]?.double ?? 0)
                        figure("middle", carlo["p50"]?.double ?? 0)
                        figure("best 5%", carlo["p95"]?.double ?? 0)
                        VStack(alignment: .leading, spacing: 1) {
                            Text("orderings that lose").font(Theme.text(10.5)).foregroundStyle(Theme.inkFaint)
                            Text("\(Int((carlo["losing_share"]?.double ?? 0) * 100))%")
                                .font(Theme.text(13, weight: .semibold)).foregroundStyle(Theme.ink)
                        }
                    }
                }
                .padding(10)
                .background(RoundedRectangle(cornerRadius: 10).fill(Theme.panel))
            }
        }
    }

    private var tradesList: some View {
        let trades = payload["trades"]?.array ?? []
        return VStack(alignment: .leading, spacing: 6) {
            Button { showTrades.toggle() } label: {
                HStack {
                    Text("\(trades.count) trades").font(Theme.text(12)).foregroundStyle(Theme.ink)
                    Image(systemName: "chevron.right").font(.system(size: 9, weight: .semibold))
                        .rotationEffect(.degrees(showTrades ? 90 : 0))
                        .foregroundStyle(Theme.inkFaint)
                    Spacer()
                }
            }
            .buttonStyle(.plain)
            if showTrades {
                ForEach(Array(trades.enumerated().reversed()), id: \.offset) { _, trade in
                    HStack(spacing: 10) {
                        Text(trade["date"]?.string ?? "").frame(width: 84, alignment: .leading)
                        Text((trade["side"]?.string ?? "").uppercased())
                            .frame(width: 38, alignment: .leading)
                            .foregroundStyle(trade["side"]?.string == "buy" ? Theme.success : Theme.clay)
                        Text(String(format: "%.4f", trade["shares"]?.double ?? 0))
                            .frame(width: 80, alignment: .trailing)
                        Text(String(format: "$%.2f", trade["price"]?.double ?? 0))
                            .frame(width: 88, alignment: .trailing)
                        if let pnl = trade["pnl"]?.double {
                            Text(String(format: "%+.2f", pnl))
                                .frame(width: 80, alignment: .trailing)
                                .foregroundStyle(pnl >= 0 ? Theme.success : Theme.danger)
                        }
                        Spacer()
                    }
                    .font(Theme.mono(10.5))
                    .foregroundStyle(Theme.inkDim)
                }
            }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 10).fill(Theme.panel))
    }

    @ViewBuilder
    private var sweepResults: some View {
        let best = payload["best"]?.object ?? [:]
        let robustness = payload["robustness"]?.double ?? 0
        VStack(alignment: .leading, spacing: 6) {
            Text("\(payload["runs"]?.int ?? 0) parameter combinations")
                .font(Theme.text(12.5, weight: .semibold)).foregroundStyle(Theme.ink)
            Text("Best: " + (best["params"]?.object.map { "\($0.key)=\($0.value.compactText.replacingOccurrences(of: "\"", with: ""))" }
                                .sorted().joined(separator: " ") ?? ""))
                .font(Theme.mono(11.5)).foregroundStyle(Theme.inkDim)
            HStack(spacing: 16) {
                figure("best return", best["return"]?.double ?? 0)
                figure("median", payload["median_return"]?.double ?? 0)
                figure("buy & hold", payload["benchmark_return"]?.double ?? 0)
                VStack(alignment: .leading, spacing: 1) {
                    Text("beat buy & hold").font(Theme.text(10.5)).foregroundStyle(Theme.inkFaint)
                    Text("\(Int(robustness * 100))% of them")
                        .font(Theme.text(13, weight: .semibold))
                        .foregroundStyle(robustness < 0.25 ? Theme.clay : Theme.ink)
                }
            }
            if robustness == 0 {
                Label("Nothing in this grid beat buy & hold. The best one here is still the wrong trade.",
                      systemImage: "exclamationmark.triangle.fill")
                    .font(Theme.text(11.5)).foregroundStyle(Theme.clay)
            } else if robustness < 0.25 {
                Label("The winner stands almost alone — that's the shape of a fluke, not an edge.",
                      systemImage: "exclamationmark.triangle.fill")
                    .font(Theme.text(11.5)).foregroundStyle(Theme.clay)
            }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 10).fill(Theme.panel))
    }

    // MARK: Paper

    private var paperTab: some View {
        let accounts = payload["accounts"]?.array ?? []
        return VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                labelled("Symbol", width: 110) {
                    TextField("AAPL", text: $symbol).textFieldStyle(.roundedBorder)
                }
                labelled("Strategy", width: 130) {
                    Picker("", selection: $strategy) {
                        ForEach(strategyNames, id: \.self) { Text($0).tag($0) }
                    }
                    .labelsHidden()
                }
                Spacer()
                Button("Update now") {
                    appState.runTrading("paper", ["paper_action": .string("step")])
                }
                .buttonStyle(JugnuButtonStyle(kind: .secondary, compact: true))
                Button("Start paper account") {
                    appState.runTrading("paper", ["paper_action": .string("start"), "symbol": .string(symbol),
                                                  "strategy": .string(strategy), "cash": .number(cash)])
                }
                .buttonStyle(JugnuButtonStyle(kind: .primary, compact: true))
            }
            .padding(10)
            .background(RoundedRectangle(cornerRadius: 10).fill(Theme.panel))

            if let failed {
                Label(failed, systemImage: "exclamationmark.circle")
                    .font(Theme.text(12)).foregroundStyle(Theme.clay)
            }
            if accounts.isEmpty {
                Text("No paper accounts yet. Start one above, and Jugnu will keep it up to date and tell you how it's doing.")
                    .font(Theme.text(12)).foregroundStyle(Theme.inkFaint)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach(accounts, id: \.compactText) { account in
                paperRow(account)
            }
        }
        .onAppear { appState.runTrading("paper", ["paper_action": .string("status")]) }
    }

    private func paperRow(_ account: JSONValue) -> some View {
        let value = account["equity"]?.double ?? 0
        let mine = account["return"]?.double ?? 0
        let theirs = account["buy_and_hold_return"]?.double ?? 0
        return HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text("\(account["symbol"]?.string ?? "") · \(account["strategy"]?.string ?? "")")
                    .font(Theme.text(12.5, weight: .medium)).foregroundStyle(Theme.ink)
                Text(account["in_market"]?.bool == true
                     ? "holding \(String(format: "%.4f", account["position"]?.double ?? 0)) @ $\(String(format: "%.2f", account["price"]?.double ?? 0))"
                     : "in cash")
                    .font(Theme.text(11)).foregroundStyle(Theme.inkFaint)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                Text("$\(String(format: "%.2f", value))").font(Theme.mono(12)).foregroundStyle(Theme.ink)
                HStack(spacing: 8) {
                    Text(percent(mine)).foregroundStyle(mine >= theirs ? Theme.success : Theme.clay)
                    Text("hold \(percent(theirs))").foregroundStyle(Theme.inkFaint)
                }
                .font(Theme.mono(10.5))
            }
            Button("Stop") {
                appState.runTrading("paper", ["paper_action": .string("stop"),
                                              "account_id": .string(account["id"]?.string ?? "")])
            }
            .buttonStyle(JugnuButtonStyle(kind: .secondary, compact: true))
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 10).fill(Theme.panel))
    }

    // MARK: Bits

    private func labelled<Content: View>(_ title: String, width: CGFloat,
                                         @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(Theme.text(10.5)).foregroundStyle(Theme.inkFaint)
            content()
        }
        .frame(width: width)
    }

    private func percent(_ value: Double) -> String {
        String(format: "%+.1f%%", value * 100)
    }
}
