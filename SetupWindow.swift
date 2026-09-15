import AppKit

/// 首次使用只引導官方登入；使用者同意前不啟動資料查詢。
final class SetupWindow: NSWindowController {
    private let onVerify: () -> Void
    private let onFinish: () -> Void
    private let onConsent: (Bool) -> Void
    private var consent: NSButton!
    private var verifyButton: NSButton!
    private var finishButton: NSButton!
    private var status: NSTextField!

    init(onVerify: @escaping () -> Void, onFinish: @escaping () -> Void, onConsent: @escaping (Bool) -> Void) {
        self.onVerify = onVerify
        self.onFinish = onFinish
        self.onConsent = onConsent
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 600, height: 610),
                              styleMask: [.titled], backing: .buffered, defer: false)
        window.title = "Subscription Pin · 開始使用"
        window.isReleasedWhenClosed = false
        super.init(window: window)
        let content = NSStackView()
        content.orientation = .vertical
        content.alignment = .leading
        content.spacing = 14
        content.translatesAutoresizingMaskIntoConstraints = false
        window.contentView?.addSubview(content)
        if let view = window.contentView {
            NSLayoutConstraint.activate([
                content.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 28),
                content.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -28),
                content.topAnchor.constraint(equalTo: view.topAnchor, constant: 24)])
        }
        func text(_ value: String, size: CGFloat = 13, bold: Bool = false) {
            let label = NSTextField(wrappingLabelWithString: value)
            label.font = .systemFont(ofSize: size, weight: bold ? .semibold : .regular)
            label.maximumNumberOfLines = 0
            content.addArrangedSubview(label)
            label.widthAnchor.constraint(equalTo: content.widthAnchor).isActive = true
        }
        func buttons(_ items: [(String, Selector)]) {
            let row = NSStackView()
            row.spacing = 8
            for item in items {
                let button = NSButton(title: item.0, target: self, action: item.1)
                button.bezelStyle = .rounded
                row.addArrangedSubview(button)
            }
            content.addArrangedSubview(row)
        }
        text("用自己的帳號，查看剩餘用量", size: 23, bold: true)
        text("安裝包不含任何人的登入資料。請先在這台 Mac 登入自己的 Codex 與 Claude Code 訂閱帳號。")
        text("1　安裝並登入 Codex", size: 15, bold: true)
        text("開啟 Codex App，以自己的 ChatGPT 訂閱帳號登入。若只使用 CLI，請先完成 codex login。")
        buttons([("開啟 Codex", #selector(openCodex)), ("Codex 安裝說明", #selector(codexGuide))])
        text("2　安裝並登入 Claude Code", size: 15, bold: true)
        text("需要 Claude Code CLI。開啟終端機，貼上 claude auth login，依瀏覽器提示登入自己的 Claude 訂閱帳號；僅登入 Claude 桌面聊天 App 可能不足。")
        buttons([("Claude Code 安裝說明", #selector(claudeGuide)),
                 ("複製登入指令", #selector(copyLogin)), ("開啟終端機", #selector(openTerminal))])
        text("3　同意讀取用量，驗證連線", size: 15, bold: true)
        text("程式會向兩個平台讀取你的用量。Claude 登入憑證僅在記憶體使用；本機只保存用量與冷卻時間。若 macOS 詢問鑰匙圈存取，請核對為本工具的 usage-helper。", size: 12)
        consent = NSButton(checkboxWithTitle: "我同意讀取這台 Mac 上自己的訂閱用量", target: self, action: #selector(consentChanged))
        content.addArrangedSubview(consent)
        let actions = NSStackView()
        actions.spacing = 10
        verifyButton = NSButton(title: "驗證連線", target: self, action: #selector(verify))
        verifyButton.bezelStyle = .rounded
        verifyButton.isEnabled = false
        finishButton = NSButton(title: "開始使用浮窗", target: self, action: #selector(finish))
        finishButton.bezelStyle = .rounded
        finishButton.isEnabled = false
        actions.addArrangedSubview(verifyButton)
        actions.addArrangedSubview(finishButton)
        let exit = NSButton(title: "稍後再設定", target: NSApp, action: #selector(NSApplication.terminate(_:)))
        exit.bezelStyle = .rounded
        actions.addArrangedSubview(exit)
        content.addArrangedSubview(actions)
        status = NSTextField(wrappingLabelWithString: "尚未讀取登入資料。至少一個平台驗證成功後，即可開始使用。")
        status.font = .systemFont(ofSize: 12)
        status.textColor = .secondaryLabelColor
        content.addArrangedSubview(status)
        status.widthAnchor.constraint(equalTo: content.widthAnchor).isActive = true
        let brand = NSStackView()
        brand.spacing = 6
        let logo = NSImageView()
        logo.image = Bundle.main.url(forResource: "yenhui-mark", withExtension: "svg").flatMap { NSImage(contentsOf: $0) }
        logo.imageScaling = .scaleProportionallyUpOrDown
        logo.widthAnchor.constraint(equalToConstant: 18).isActive = true
        logo.heightAnchor.constraint(equalToConstant: 18).isActive = true
        logo.setAccessibilityLabel("言回 logo")
        brand.addArrangedSubview(logo)
        let developer = NSTextField(labelWithString: "言回有限公司開發")
        developer.font = .systemFont(ofSize: 11)
        developer.textColor = .secondaryLabelColor
        brand.addArrangedSubview(developer)
        content.addArrangedSubview(brand)
        window.contentView?.layoutSubtreeIfNeeded()
        window.setContentSize(NSSize(width: 600, height: max(610, content.fittingSize.height + 48)))
        window.center()
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    @objc private func consentChanged() {
        let allowed = consent.state == .on
        verifyButton.isEnabled = allowed
        if !allowed { finishButton.isEnabled = false }
        onConsent(allowed)
    }
    @objc private func verify() {
        guard consent.state == .on else { return }
        verifyButton.isEnabled = false
        status.stringValue = "正在驗證，請稍候。查詢仍遵守冷卻期限，不會因連按而密集重試。"
        onVerify()
    }
    func update(_ snapshot: Snapshot?, failure: String?) {
        verifyButton.isEnabled = consent.state == .on
        let providers = snapshot?.providers ?? []
        finishButton.isEnabled = consent.state == .on && providers.contains { $0.error == nil && !$0.cards.isEmpty && !$0.isStale }
        status.stringValue = failure ?? ["codex", "claude"].map { id in
            let name = id == "codex" ? "Codex" : "Claude Code"
            guard let p = providers.first(where: { $0.provider == id }) else { return name + "：尚未取得資料" }
            return name + "：" + (p.error ?? (p.isStale ? "資料已過期，等待下次查詢" : "已驗證，可讀取本機帳號用量"))
        }.joined(separator: "\n")
    }
    @objc private func finish() { guard finishButton.isEnabled else { return }; onFinish() }
    @objc private func codexGuide() { open("https://developers.openai.com/codex/app/") }
    @objc private func claudeGuide() { open("https://code.claude.com/docs/en/quickstart") }
    private func open(_ string: String) { if let url = URL(string: string) { NSWorkspace.shared.open(url) } }
    @objc private func openCodex() {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let paths = ["/Applications/Codex.app", "/Applications/ChatGPT.app", home + "/Applications/Codex.app", home + "/Applications/ChatGPT.app"]
        if let path = paths.first(where: { FileManager.default.fileExists(atPath: $0) }) {
            NSWorkspace.shared.open(URL(fileURLWithPath: path))
        } else { codexGuide() }
    }
    @objc private func openTerminal() { NSWorkspace.shared.open(URL(fileURLWithPath: "/System/Applications/Utilities/Terminal.app")) }
    @objc private func copyLogin() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString("claude auth login", forType: .string)
        status.stringValue = "已複製 claude auth login。請到終端機貼上執行，完成自己的帳號登入後再驗證。"
    }
}
