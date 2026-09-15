import AppKit

struct QuotaWindow: Decodable {
    let label: String
    let remaining: Double
    let resetsAt: Double?
}
struct QuotaCard: Decodable {
    let id: String
    let name: String
    let windows: [QuotaWindow]
}
struct Provider: Decodable {
    let provider: String
    let cards: [QuotaCard]
    let updatedAt: Double?
    let error: String?
    let nextAllowedAt: Double?
    let staleAfter: Double?
    let rateLimited: Bool?
    let cached: Bool?
    var isStale: Bool {
        updatedAt.map { Date().timeIntervalSince1970 - $0 > (staleAfter ?? 180) } ?? true
    }
}
struct Snapshot: Decodable { let providers: [Provider] }

final class UsagePanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

final class DragTitle: NSTextField {
    private var startPoint: NSPoint?
    private var startOrigin: NSPoint?
    override var mouseDownCanMoveWindow: Bool { false }
    override func mouseDown(with event: NSEvent) {
        guard let window = window else { return }
        startPoint = window.convertPoint(toScreen: event.locationInWindow)
        startOrigin = window.frame.origin
    }
    override func mouseDragged(with event: NSEvent) {
        guard let window = window, window.isMovableByWindowBackground,
              let point = startPoint, let origin = startOrigin else { return }
        let current = window.convertPoint(toScreen: event.locationInWindow)
        window.setFrameOrigin(NSPoint(x: origin.x + current.x - point.x, y: origin.y + current.y - point.y))
    }
    override func mouseUp(with event: NSEvent) {
        guard let window = window else { return }
        if ProcessInfo.processInfo.environment["SUBSCRIPTION_PIN_UI_TEST"] == "1" {
            print("DRAG locked=\(!window.isMovableByWindowBackground) moved=\(startOrigin != window.frame.origin)")
            fflush(stdout)
        }
        startPoint = nil; startOrigin = nil
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private var panel: UsagePanel!
    private var statusItem: NSStatusItem!
    private var stack: NSStackView!
    private var timer: Timer?
    private var displayTimer: Timer?
    private var task: Process?
    private var snapshot: Snapshot?
    private var loading = false
    private var failure: String?
    private var nextRefresh = Date.distantPast
    private var lastAttempt = Date.distantPast
    private var setupWindow: SetupWindow?
    private let defaults = UserDefaults.standard
    private var topmost: Bool { defaults.bool(forKey: "topmost") }
    private var locked: Bool { defaults.bool(forKey: "locked") }
    private var showExtra: Bool { defaults.bool(forKey: "showExtra") }
    private var compact: Bool { defaults.bool(forKey: "compact") }

    func applicationDidFinishLaunching(_ notification: Notification) {
        defaults.register(defaults: ["topmost": true, "locked": false, "showExtra": false, "compact": true])
        NSApp.setActivationPolicy(.accessory)
        createStatusItem()
        panel = UsagePanel(contentRect: NSRect(x: 0, y: 0, width: 360, height: 350),
                           styleMask: [.borderless, .nonactivatingPanel],
                           backing: .buffered, defer: false)
        panel.title = "Subscription Pin"
        panel.titleVisibility = .hidden
        panel.titlebarAppearsTransparent = true
        panel.standardWindowButton(.closeButton)?.isHidden = true
        panel.standardWindowButton(.miniaturizeButton)?.isHidden = true
        panel.standardWindowButton(.zoomButton)?.isHidden = true
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.delegate = self
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        applyWindowPreferences()
        restorePosition()
        render()
        if defaults.bool(forKey: "setupCompleted") && defaults.bool(forKey: "usageConsent") {
            panel.orderFrontRegardless()
            refresh()
        } else {
            showSetup()
        }
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            guard let self = self, Date() >= self.nextRefresh else { return }
            self.refresh()
        }
        displayTimer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in
            self?.render()
        }
        NSWorkspace.shared.notificationCenter.addObserver(self, selector: #selector(wake),
                name: NSWorkspace.didWakeNotification, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(screensChanged),
                name: NSApplication.didChangeScreenParametersNotification, object: nil)
    }

    private func createStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "C —  A —"
        statusItem.button?.font = .monospacedDigitSystemFont(ofSize: 11, weight: .medium)
        rebuildMenu()
    }

    private func menuItem(_ title: String, _ action: Selector, checked: Bool? = nil) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.target = self
        if let checked = checked { item.state = checked ? .on : .off }
        return item
    }

    private func rebuildMenu() {
        let menu = NSMenu()
        menu.addItem(menuItem("顯示／隱藏浮窗", #selector(toggleVisible)))
        menu.addItem(menuItem("立即更新", #selector(manualRefresh)))
        menu.addItem(menuItem("登入與使用引導", #selector(showSetup)))
        menu.addItem(.separator())
        menu.addItem(menuItem("保持最上層", #selector(toggleTopmost), checked: topmost))
        menu.addItem(menuItem("鎖定位置", #selector(toggleLock), checked: locked))
        menu.addItem(menuItem("精簡版", #selector(toggleCompact), checked: compact))
        menu.addItem(menuItem("顯示 Codex 其他模型額度", #selector(toggleExtra), checked: showExtra))
        menu.addItem(menuItem("移回螢幕右上角", #selector(resetPosition)))
        menu.addItem(.separator())
        menu.addItem(menuItem("結束 Subscription Pin", #selector(quit)))
        statusItem.menu = menu
    }

    private func label(_ text: String, size: CGFloat = 12, weight: NSFont.Weight = .regular,
                       color: NSColor = .labelColor) -> NSTextField {
        let field = NSTextField(labelWithString: text)
        field.font = .systemFont(ofSize: size, weight: weight)
        field.textColor = color
        field.lineBreakMode = .byTruncatingTail
        field.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return field
    }

    private func button(_ title: String, symbol: String, action: Selector, active: Bool = false) -> NSButton {
        let b = NSButton(title: title, target: self, action: action)
        b.image = NSImage(systemSymbolName: symbol, accessibilityDescription: title)
        b.imagePosition = .imageLeading
        b.bezelStyle = .recessed
        b.font = .systemFont(ofSize: 11)
        b.contentTintColor = active ? NSColor.systemBlue : .secondaryLabelColor
        b.toolTip = title
        b.setAccessibilityLabel(title)
        return b
    }

    private func row(_ views: [NSView], spacing: CGFloat = 6) -> NSStackView {
        let s = NSStackView(views: views)
        s.orientation = .horizontal
        s.alignment = .centerY
        s.spacing = spacing
        return s
    }

    private func spacer() -> NSView {
        let v = NSView()
        v.setContentHuggingPriority(.defaultLow, for: .horizontal)
        return v
    }

    private func fullWidth(_ view: NSView, in parent: NSStackView) {
        parent.addArrangedSubview(view)
        view.widthAnchor.constraint(equalTo: parent.widthAnchor).isActive = true
    }

    private func dateText(_ epoch: Double) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "zh_TW")
        f.timeZone = .current
        f.dateFormat = "M/d（EEE）HH:mm"
        return f.string(from: Date(timeIntervalSince1970: ceil(epoch)))
    }

    private func timeText(_ epoch: Double) -> String {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        return f.string(from: Date(timeIntervalSince1970: epoch))
    }

    private func countdown(_ epoch: Double) -> String {
        let minutes = max(0, Int(ceil((epoch - Date().timeIntervalSince1970) / 60)))
        let days = minutes / 1440, hours = (minutes % 1440) / 60
        let dayText = days > 0 ? "\(days)日" : ""
        let hourText = hours > 0 || days > 0 ? "\(hours)時" : ""
        return "\(dayText)\(hourText)\(minutes % 60)分"
    }

    private func quotaView(_ quota: QuotaWindow, stale: Bool) -> NSView {
        let s = NSStackView()
        s.orientation = .vertical
        s.alignment = .leading
        s.spacing = 3
        let resetPassed = quota.resetsAt.map { $0 <= Date().timeIntervalSince1970 } ?? false
        let invalid = stale || resetPassed
        let color: NSColor = invalid ? .tertiaryLabelColor :
            quota.remaining <= 10 ? .systemRed : quota.remaining <= 25 ? .systemOrange : .labelColor
        let percent = label(invalid ? "—" : String(format: "%.0f%%", floor(quota.remaining)),
                            size: 27, weight: .semibold, color: color)
        percent.font = .monospacedDigitSystemFont(ofSize: 27, weight: .semibold)
        percent.setContentCompressionResistancePriority(.required, for: .horizontal)
        fullWidth(row([label(quota.label, size: 12, weight: .medium), spacer(),
                       label("剩餘", size: 10, color: .secondaryLabelColor), percent]), in: s)
        let progress = NSProgressIndicator()
        progress.style = .bar
        progress.isIndeterminate = false
        progress.minValue = 0
        progress.maxValue = 100
        progress.doubleValue = invalid ? 0 : quota.remaining
        progress.controlSize = .small
        progress.setAccessibilityLabel("\(quota.label)剩餘額度")
        fullWidth(progress, in: s)
        let reset = quota.resetsAt.map { "\(countdown($0))後重置" } ?? "尚未提供重置時間"
        fullWidth(label(reset, size: 11, color: .secondaryLabelColor), in: s)
        let detail = stale ? "資料已過期，正在等待更新" : resetPassed ? "重置時間已到，等待服務確認" : ""
        fullWidth(label(detail, size: 10, color: .tertiaryLabelColor), in: s)
        return s
    }

    private func cardView(name: String, windows: [QuotaWindow], updated: Double?, error: String?, staleAfter: Double = 180) -> NSView {
        let box = NSView()
        box.wantsLayer = true
        box.layer?.cornerRadius = 12
        box.layer?.backgroundColor = NSColor.controlBackgroundColor.withAlphaComponent(0.8).cgColor
        let inner = NSStackView()
        inner.orientation = .vertical
        inner.alignment = .leading
        inner.spacing = 9
        inner.translatesAutoresizingMaskIntoConstraints = false
        box.addSubview(inner)
        NSLayoutConstraint.activate([
            inner.leadingAnchor.constraint(equalTo: box.leadingAnchor, constant: 14),
            inner.trailingAnchor.constraint(equalTo: box.trailingAnchor, constant: -14),
            inner.topAnchor.constraint(equalTo: box.topAnchor, constant: 12),
            inner.bottomAnchor.constraint(equalTo: box.bottomAnchor, constant: -12)])
        let stale = updated.map { Date().timeIntervalSince1970 - $0 > staleAfter } ?? true
        let updateText = updated.map { timeText($0) } ?? (loading ? "讀取中" : "未連線")
        fullWidth(row([label(name, size: 14, weight: .semibold), spacer(),
                       label(updateText, size: 10, color: .secondaryLabelColor)]), in: inner)
        if let error = error {
            let text = NSTextField(wrappingLabelWithString: error)
            text.font = .systemFont(ofSize: 12)
            text.textColor = .secondaryLabelColor
            fullWidth(text, in: inner)
        } else if windows.isEmpty {
            fullWidth(label(loading ? "正在讀取訂閱額度…" : "尚未提供額度", color: .secondaryLabelColor), in: inner)
        } else {
            for (index, quota) in windows.enumerated() {
                if index > 0 {
                    let divider = NSBox(); divider.boxType = .separator
                    fullWidth(divider, in: inner)
                }
                fullWidth(quotaView(quota, stale: stale), in: inner)
            }
        }
        return box
    }

    private func render() {
        guard panel != nil else { return }
        if compact { renderCompact(); return }
        let top = panel.frame.maxY
        let background = NSVisualEffectView()
        background.material = .hudWindow
        background.blendingMode = .behindWindow
        background.state = .active
        background.menu = statusItem.menu
        background.wantsLayer = true
        background.layer?.cornerRadius = 16
        background.layer?.masksToBounds = true
        stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 10
        stack.translatesAutoresizingMaskIntoConstraints = false
        background.addSubview(stack)
        panel.contentView = background
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: background.leadingAnchor, constant: 14),
            stack.trailingAnchor.constraint(equalTo: background.trailingAnchor, constant: -14),
            stack.topAnchor.constraint(equalTo: background.topAnchor, constant: 14),
            stack.bottomAnchor.constraint(equalTo: background.bottomAnchor, constant: -14)])
        let title = DragTitle(labelWithString: "訂閱用量")
        title.font = .systemFont(ofSize: 14, weight: .semibold)
        title.toolTip = locked ? "先解除鎖定即可移動" : "拖曳這裡移動浮窗"
        fullWidth(row([title, spacer(),
            button("精簡", symbol: "arrow.down.right.and.arrow.up.left", action: #selector(toggleCompact)),
            button(topmost ? "已置頂" : "置頂", symbol: topmost ? "pin.fill" : "pin", action: #selector(toggleTopmost), active: topmost),
            button(locked ? "已鎖定" : "鎖定", symbol: locked ? "lock.fill" : "lock.open", action: #selector(toggleLock), active: locked)]), in: stack)
        for id in ["codex", "claude"] {
            let provider = snapshot?.providers.first { $0.provider == id }
            let cards = provider?.cards.filter { id != "codex" || showExtra || $0.id == "codex" } ?? []
            let error = failure ?? provider?.error
            if cards.isEmpty || error != nil {
                fullWidth(cardView(name: id == "codex" ? "Codex" : "Claude Code", windows: [],
                                   updated: nil, error: error), in: stack)
            } else {
                for card in cards {
                    fullWidth(cardView(name: card.name, windows: card.windows,
                                       updated: provider?.updatedAt, error: nil,
                                       staleAfter: provider?.staleAfter ?? 180), in: stack)
                }
            }
        }
        let refreshButton = button(loading ? "更新中" : "更新", symbol: "arrow.clockwise", action: #selector(manualRefresh))
        refreshButton.isEnabled = !loading
        let note = queryStatus
        fullWidth(row([label(note, size: 10, color: .secondaryLabelColor), spacer(), refreshButton,
                       button("收起", symbol: "minus", action: #selector(toggleVisible))]), in: stack)
        fullWidth(label("手動更新也遵守查詢間隔 · 倒數在本機更新", size: 10, color: .tertiaryLabelColor), in: stack)
        fullWidth(row([brandLogo(size: 16), label("言回有限公司開發", size: 10, color: .secondaryLabelColor)]), in: stack)
        // 依真實資料列改高度，視窗頂端與使用者拖曳位置保持不變。
        background.layoutSubtreeIfNeeded()
        let height = max(240, stack.fittingSize.height + 28)
        panel.setFrame(NSRect(x: panel.frame.minX, y: top - height, width: 360, height: height), display: true)
        keepOnScreen()
        updateMenuNumbers()
    }

    private func renderCompact() {
        let providerIds = ["codex", "claude"]
        let visibleWindows = providerIds.map { id -> [QuotaWindow] in
            let card = snapshot?.providers.first { $0.provider == id }?.cards.first { $0.id == id }
            let rows = card?.windows ?? []
            if id == "claude" { return rows.filter { ["5 小時", "每週", "Fable 每週"].contains($0.label) } }
            return Array(rows.prefix(2))
        }
        let rowCount = visibleWindows.reduce(0) { $0 + max(1, $1.count) }
        let height: CGFloat = 160 + CGFloat(max(0, rowCount - 4) * 24)
        let top = panel.frame.maxY
        let background = NSVisualEffectView(frame: NSRect(x: 0, y: 0, width: 180, height: height))
        background.material = .hudWindow
        background.blendingMode = .behindWindow
        background.state = .active
        background.menu = statusItem.menu
        background.wantsLayer = true
        background.layer?.cornerRadius = 12
        background.layer?.masksToBounds = true
        panel.contentView = background
        let title = DragTitle(labelWithString: "剩餘 · 重置倒數")
        title.font = .systemFont(ofSize: 9, weight: .medium)
        title.textColor = .secondaryLabelColor
        title.frame = NSRect(x: 8, y: height - 23, width: 83, height: 16)
        title.toolTip = locked ? "先解除鎖定即可移動" : "拖曳這裡移動浮窗"
        background.addSubview(title)
        let controls: [(String, String, Selector, Bool)] = [
            (topmost ? "已置頂" : "置頂", topmost ? "pin.fill" : "pin", #selector(toggleTopmost), topmost),
            (locked ? "已鎖定" : "鎖定", locked ? "lock.fill" : "lock.open", #selector(toggleLock), locked),
            ("展開完整資訊", "arrow.up.left.and.arrow.down.right", #selector(toggleCompact), false),
            (loading ? "更新中" : "更新", "arrow.clockwise", #selector(manualRefresh), false)]
        for (index, item) in controls.enumerated() {
            let control = button(item.0, symbol: item.1, action: item.2, active: item.3)
            control.title = ""
            control.imagePosition = .imageOnly
            control.frame = NSRect(x: CGFloat(91 + index * 20), y: height - 25, width: 20, height: 20)
            if index == 3 { control.isEnabled = !loading }
            background.addSubview(control)
        }
        func addText(_ text: String, _ frame: NSRect, _ size: CGFloat,
                     _ weight: NSFont.Weight = .regular, _ color: NSColor = .labelColor) -> NSTextField {
            let view = label(text, size: size, weight: weight, color: color)
            view.frame = frame
            view.maximumNumberOfLines = 1
            background.addSubview(view)
            return view
        }
        func divider(_ y: CGFloat) {
            let line = NSBox(frame: NSRect(x: 8, y: y, width: 164, height: 1))
            line.boxType = .separator
            background.addSubview(line)
        }
        divider(height - 30)
        var cursorY = height - 57
        for (index, id) in providerIds.enumerated() {
            let name = id == "codex" ? "Codex" : "Claude Code"
            let provider = snapshot?.providers.first { $0.provider == id }
            let icon = NSImageView(frame: NSRect(x: 8, y: cursorY + 4, width: 18, height: 18))
            icon.image = Bundle.main.path(forResource: id, ofType: "png").flatMap { NSImage(contentsOfFile: $0) }
                ?? NSImage(systemSymbolName: "terminal", accessibilityDescription: name)
            icon.imageScaling = .scaleProportionallyUpOrDown
            icon.setAccessibilityLabel(name)
            icon.toolTip = name + (provider?.error.map { "：" + $0 } ?? "") +
                (provider?.updatedAt.map { "；資料更新於 " + timeText($0) } ?? "") +
                (provider?.nextAllowedAt.map { "；下次查詢：" + countdown($0) + "後" } ?? "")
            background.addSubview(icon)
            if let error = failure ?? provider?.error {
                let message = provider?.rateLimited == true ? "查詢冷卻 · \(provider?.nextAllowedAt.map(countdown) ?? "稍後")" :
                    error.contains("登入") ? "請更新登入" : "暫時無法更新"
                let field = addText(message,
                    NSRect(x: 31, y: cursorY + 5, width: 141, height: 18), 11, .medium, .secondaryLabelColor)
                field.toolTip = error
                cursorY -= 24
            } else if visibleWindows[index].isEmpty {
                _ = addText(loading ? "讀取中…" : "尚未提供額度", NSRect(x: 31, y: cursorY + 5, width: 141, height: 18), 11, .regular, .secondaryLabelColor)
                cursorY -= 24
            } else {
                for quota in visibleWindows[index] {
                    let stale = provider?.isStale ?? true
                    let pastReset = quota.resetsAt.map { $0 <= Date().timeIntervalSince1970 } ?? false
                    let invalid = stale || pastReset
                    let color: NSColor = invalid ? .tertiaryLabelColor : quota.remaining <= 10 ? .systemRed : quota.remaining <= 25 ? .systemOrange : .labelColor
                    let shortLabel = quota.label == "Fable 每週" ? "Fable" : quota.label
                    _ = addText(shortLabel, NSRect(x: 30, y: cursorY + 7, width: 32, height: 13), 9, .regular, .secondaryLabelColor)
                    let value = addText(invalid ? "—" : String(format: "%.0f%%", floor(quota.remaining)),
                        NSRect(x: 60, y: cursorY + 1, width: 46, height: 24), 16, .semibold, color)
                    value.font = .monospacedDigitSystemFont(ofSize: quota.remaining >= 100 ? 14 : 16, weight: .semibold)
                    let remainingTime = invalid ? "等待更新" : quota.resetsAt.map(countdown) ?? "未提供"
                    let reset = addText(remainingTime, NSRect(x: 108, y: cursorY + 7, width: 64, height: 13), 9, .regular, .secondaryLabelColor)
                    reset.toolTip = invalid ? "正在等待服務確認重置" : "\(remainingTime)後重置"
                    reset.setAccessibilityLabel("\(name) \(quota.label)，\(remainingTime)後重置")
                    value.toolTip = "\(name) \(quota.label)剩餘額度"
                    cursorY -= 24
                }
            }
            if index == 0 { divider(cursorY + 19); cursorY -= 8 }
        }
        divider(17)
        let status = loading ? "更新中…" : failure != nil ? "更新失敗" : queryStatus
        let footer = addText(status, NSRect(x: 8, y: 3, width: 146, height: 12), 9, .regular, .tertiaryLabelColor)
        footer.toolTip = "Codex 至少間隔 1 分鐘、Claude 至少間隔 5 分鐘查詢；手動更新、重開與喚醒共用冷卻期限。倒數每 15 秒在本機更新。"
        let brand = brandLogo(size: 12)
        brand.frame = NSRect(x: 160, y: 3, width: 12, height: 12)
        background.addSubview(brand)
        panel.setFrame(NSRect(x: panel.frame.minX, y: top - height, width: 180, height: height), display: true)
        keepOnScreen()
        updateMenuNumbers()
    }

    private func updateMenuNumbers() {
        func number(_ id: String) -> String {
            guard failure == nil, let p = snapshot?.providers.first(where: { $0.provider == id }),
                  p.error == nil, !p.isStale else { return "—" }
            let rows = p.cards.filter { id != "codex" || $0.id == "codex" }.flatMap { $0.windows }
                .filter { id != "claude" || !$0.label.contains("Fable") }
            guard !rows.isEmpty, rows.allSatisfy({ $0.resetsAt.map { $0 > Date().timeIntervalSince1970 } ?? true }),
                  let value = rows.map({ $0.remaining }).min() else { return "—" }
            return String(format: "%.0f%%", floor(value))
        }
        var fable = ""
        if failure == nil, let p = snapshot?.providers.first(where: { $0.provider == "claude" }), p.error == nil,
           let quota = p.cards.flatMap({ $0.windows }).first(where: { $0.label == "Fable 每週" }) {
            let valid = !p.isStale &&
                (quota.resetsAt.map { $0 > Date().timeIntervalSince1970 } ?? true)
            fable = valid ? String(format: "  F %.0f%%", floor(quota.remaining)) : "  F —"
        }
        statusItem.button?.title = "C \(number("codex"))  A \(number("claude"))\(fable)"
        statusItem.button?.toolTip = "C：Codex；A：Claude 一般額度；F：Fable 獨立額度。點選開啟浮窗"
    }

    private var queryStatus: String {
        if let p = snapshot?.providers.first(where: { $0.rateLimited == true }), let next = p.nextAllowedAt {
            return "\(p.provider == "claude" ? "Claude" : "Codex") 冷卻 \(countdown(next))"
        }
        return "Codex 1分 · Claude 5分"
    }

    private func brandLogo(size: CGFloat) -> NSImageView {
        let logo = NSImageView()
        logo.image = Bundle.main.url(forResource: "yenhui-mark", withExtension: "svg").flatMap { NSImage(contentsOf: $0) }
        logo.imageScaling = .scaleProportionallyUpOrDown
        logo.widthAnchor.constraint(equalToConstant: size).isActive = true
        logo.heightAnchor.constraint(equalToConstant: size).isActive = true
        logo.toolTip = "言回有限公司開發"
        logo.setAccessibilityLabel("言回有限公司開發")
        return logo
    }

    private func refresh() {
        guard defaults.bool(forKey: "usageConsent"), !loading else { return }
        loading = true
        lastAttempt = Date()
        nextRefresh = Date().addingTimeInterval(60)
        render()
        let p = Process()
        if let resources = Bundle.main.resourceURL,
           FileManager.default.isExecutableFile(atPath: resources.appendingPathComponent("usage-helper/usage-helper").path) {
            p.executableURL = resources.appendingPathComponent("usage-helper/usage-helper")
            p.arguments = []
        } else if let script = Bundle.main.path(forResource: "usage", ofType: "py") {
            p.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
            p.arguments = [script]
        } else {
            loading = false; failure = "找不到用量元件，請重新安裝程式"; render(); return
        }
        p.currentDirectoryURL = FileManager.default.homeDirectoryForCurrentUser
        let output = Pipe()
        p.standardOutput = output
        p.standardError = FileHandle.nullDevice
        task = p
        DispatchQueue.global(qos: .utility).async { [weak self] in
            do {
                try p.run()
                // adapter 已限制各查詢逾時；此處仍提供整體保護。
                DispatchQueue.global().asyncAfter(deadline: .now() + 45) {
                    if p.isRunning { p.terminate() }
                }
                let data = output.fileHandleForReading.readDataToEndOfFile()
                p.waitUntilExit()
                let result = p.terminationStatus == 0 ? try? JSONDecoder().decode(Snapshot.self, from: data) : nil
                DispatchQueue.main.async {
                    guard let self = self else { return }
                    self.loading = false; self.task = nil
                    self.snapshot = result
                    self.failure = result == nil ? "無法取得用量，請檢查網路後按更新" : nil
                    let now = Date().timeIntervalSince1970
                    let due = result?.providers.compactMap { $0.nextAllowedAt }.min() ?? (now + 60)
                    self.nextRefresh = Date(timeIntervalSince1970: max(now + 5, due))
                    self.setupWindow?.update(result, failure: self.failure)
                    self.render()
                }
            } catch {
                DispatchQueue.main.async {
                    self?.loading = false; self?.task = nil
                    self?.failure = "無法啟動用量元件，請重新安裝完整的 App"
                    self?.setupWindow?.update(nil, failure: self?.failure)
                    self?.render()
                }
            }
        }
    }

    @objc private func showSetup() {
        if setupWindow == nil {
            setupWindow = SetupWindow(onVerify: { [weak self] in
                guard let self = self else { return }
                self.defaults.set(true, forKey: "usageConsent")
                self.refresh()
            }, onFinish: { [weak self] in
                guard let self = self else { return }
                self.defaults.set(true, forKey: "setupCompleted")
                self.setupWindow?.window?.orderOut(nil)
                self.panel.orderFrontRegardless()
            }, onConsent: { [weak self] allowed in
                self?.defaults.set(allowed, forKey: "usageConsent")
            })
        }
        setupWindow?.showWindow(nil)
        setupWindow?.window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func applyWindowPreferences() {
        panel.level = topmost ? .floating : .normal
        // 無原生標題列；只切換背景拖曳，保留輔助使用的視窗定位能力。
        panel.isMovable = true
        panel.isMovableByWindowBackground = !locked
    }

    private func restorePosition() {
        if defaults.object(forKey: "positionX") != nil {
            let x = defaults.double(forKey: "positionX"), top = defaults.double(forKey: "positionTop")
            panel.setFrameTopLeftPoint(NSPoint(x: x, y: top))
        } else { resetPosition() }
        keepOnScreen()
    }

    private func keepOnScreen() {
        guard let screen = NSScreen.screens.first(where: { $0.visibleFrame.intersects(panel.frame) }) ?? NSScreen.main else { return }
        var frame = panel.frame
        frame.origin.x = min(max(frame.minX, screen.visibleFrame.minX), screen.visibleFrame.maxX - frame.width)
        frame.origin.y = min(max(frame.minY, screen.visibleFrame.minY), screen.visibleFrame.maxY - frame.height)
        if panel.frame != frame { panel.setFrame(frame, display: true) }
    }

    func windowDidMove(_ notification: Notification) {
        defaults.set(panel.frame.minX, forKey: "positionX")
        defaults.set(panel.frame.maxY, forKey: "positionTop")
    }
    func windowShouldClose(_ sender: NSWindow) -> Bool { sender.orderOut(nil); return false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !defaults.bool(forKey: "setupCompleted") { showSetup(); return true }
        keepOnScreen(); panel.orderFrontRegardless(); return true
    }
    @objc private func manualRefresh() {
        guard Date().timeIntervalSince(lastAttempt) >= 10 else { return }
        // adapter 的持久化排程統一控制連網；手動更新只讀尚在冷卻的快取。
        refresh()
    }
    @objc private func wake() { nextRefresh = .distantPast; refresh() }
    @objc private func screensChanged() { keepOnScreen() }
    @objc private func toggleTopmost() { defaults.set(!topmost, forKey: "topmost"); applyWindowPreferences(); rebuildMenu(); render() }
    @objc private func toggleLock() { defaults.set(!locked, forKey: "locked"); applyWindowPreferences(); rebuildMenu(); render() }
    @objc private func toggleExtra() {
        defaults.set(!showExtra, forKey: "showExtra")
        if showExtra { defaults.set(false, forKey: "compact") }
        rebuildMenu(); render()
    }
    @objc private func toggleCompact() { defaults.set(!compact, forKey: "compact"); rebuildMenu(); render() }
    @objc private func toggleVisible() {
        if panel.isVisible { panel.orderOut(nil) }
        else { keepOnScreen(); panel.orderFrontRegardless() }
    }
    @objc private func resetPosition() {
        guard let screen = NSScreen.main else { return }
        panel.setFrameTopLeftPoint(NSPoint(x: screen.visibleFrame.maxX - 380, y: screen.visibleFrame.maxY - 20))
    }
    @objc private func quit() { NSApp.terminate(nil) }
    func applicationWillTerminate(_ notification: Notification) {
        timer?.invalidate(); displayTimer?.invalidate()
        if let task = task, task.isRunning { task.terminate() }
    }
}

let app = NSApplication.shared
if let identifier = Bundle.main.bundleIdentifier,
   NSRunningApplication.runningApplications(withBundleIdentifier: identifier).count > 1 {
    exit(0)
}
let delegate = AppDelegate()
app.delegate = delegate
app.run()
