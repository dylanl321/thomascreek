import SwiftUI
import Observation
import Speech
import AVFoundation

// Ask Thomas Creek: a floating mic on every screen opens a half-height panel. The screen behind stays
// visible and usable. Talk (speech is turned into text on the phone) or type; the hub does the rest.

@MainActor
@Observable
final class AssistantModel {
    static let shared = AssistantModel()

    struct Line: Identifiable, Hashable {
        enum Kind: Hashable { case me, house, error }
        let id = UUID()
        var kind: Kind
        var text: String
        var steps: [AskReply.Step] = []
    }
    struct Card: Identifiable, Hashable {
        var confirm: AskReply.Confirm
        var state: State = .waiting
        enum State: Hashable { case waiting, running, done, cancelled, failed(String) }
        var id: String { confirm.id }
    }

    var lines: [Line] = []
    var cards: [Card] = []
    var draft = ""
    var busy = false
    var listening = false
    var partial = ""
    var ready = false
    var speakReplies = true
    private var conversation: String?
    private var outcomes: [(id: String, ok: Bool, message: String?)] = []
    private var usedVoice = false

    private let synth = AVSpeechSynthesizer()
    private let engine = AVAudioEngine()
    private var recognizer: SFSpeechRecognizer? = SFSpeechRecognizer(locale: Locale.current) ?? SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var silence: Task<Void, Never>?

    func checkReady() async { ready = await HubClient.shared.assistantReady() }

    func greeting(for name: String?, kid: Bool) -> String {
        kid ? "Hi \(name ?? "there")! What do you want to do?" : "Hi \(name ?? "there"). What can I do?"
    }

    // MARK: asking

    func send(_ text: String, screen: String?, voice: Bool = false) {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty, !busy else { return }
        draft = ""
        usedVoice = voice
        lines.append(Line(kind: .me, text: t))
        busy = true
        let done = outcomes
        outcomes = []
        Task {
            do {
                let r = try await HubClient.shared.ask(t, conversation: conversation, screen: screen, confirmed: done)
                conversation = r.conversationID
                lines.append(Line(kind: .house, text: r.reply, steps: r.steps.filter { $0.status != "confirm" }))
                cards += r.confirms.map { Card(confirm: $0) }
                if r.changed { Task { await HomeStore.shared.refreshAll() } }
                if voice && speakReplies { speak(r.reply) }
            } catch {
                outcomes = done + outcomes
                lines.append(Line(kind: .error, text: (error as? LocalizedError)?.errorDescription ?? "Something went wrong."))
            }
            busy = false
        }
    }

    func confirm(_ card: Card) {
        guard let i = cards.firstIndex(where: { $0.id == card.id }) else { return }
        cards[i].state = .running
        Task {
            do {
                try await HubClient.shared.perform(card.confirm)
                set(card.id, .done)
                outcomes.append((card.id, true, nil))
                Task { await HomeStore.shared.refreshAll() }
            } catch HubClient.HubError.cancelled {
                set(card.id, .waiting)
            } catch {
                let m = (error as? LocalizedError)?.errorDescription ?? "It didn't work."
                set(card.id, .failed(m))
                outcomes.append((card.id, false, m))
            }
        }
    }

    func cancel(_ card: Card) {
        set(card.id, .cancelled)
        outcomes.append((card.id, false, "cancelled by the person"))
    }

    private func set(_ id: String, _ s: Card.State) {
        if let i = cards.firstIndex(where: { $0.id == id }) { cards[i].state = s }
    }

    func newConversation() {
        stopListening(send: false)
        synth.stopSpeaking(at: .immediate)
        lines = []; cards = []; outcomes = []; conversation = nil
    }

    // MARK: voice

    func toggleListening(screen: String?) {
        if listening { stopListening(send: true, screen: screen) } else { Task { await startListening(screen: screen) } }
    }

    func startListening(screen: String?) async {
        guard !listening else { return }
        synth.stopSpeaking(at: .immediate)
        let speechOK = await withCheckedContinuation { c in SFSpeechRecognizer.requestAuthorization { c.resume(returning: $0 == .authorized) } }
        let micOK = await AVAudioApplication.requestRecordPermission()
        guard speechOK, micOK, let recognizer, recognizer.isAvailable else {
            lines.append(Line(kind: .error, text: "Turn on Microphone and Speech Recognition for Thomas Creek in Settings to talk."))
            return
        }
        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playAndRecord, mode: .measurement, options: [.duckOthers, .defaultToSpeaker])
            try session.setActive(true, options: .notifyOthersOnDeactivation)
            let req = SFSpeechAudioBufferRecognitionRequest()
            req.shouldReportPartialResults = true
            // Keep audio on the phone where the phone can do it.
            if recognizer.supportsOnDeviceRecognition { req.requiresOnDeviceRecognition = true }
            req.addsPunctuation = true
            request = req
            let input = engine.inputNode
            input.removeTap(onBus: 0)
            input.installTap(onBus: 0, bufferSize: 1024, format: input.outputFormat(forBus: 0)) { buf, _ in req.append(buf) }
            engine.prepare()
            try engine.start()
            partial = ""
            listening = true
            Haptics.tap()
            task = recognizer.recognitionTask(with: req) { [weak self] result, error in
                Task { @MainActor in
                    guard let self, self.listening else { return }
                    if let result {
                        self.partial = result.bestTranscription.formattedString
                        if result.isFinal { self.stopListening(send: true, screen: screen); return }
                        self.armSilence(screen: screen)
                    } else if error != nil {
                        self.stopListening(send: !self.partial.isEmpty, screen: screen)
                    }
                }
            }
            armSilence(screen: screen, first: true)
        } catch {
            stopListening(send: false)
            lines.append(Line(kind: .error, text: "The microphone isn't available right now."))
        }
    }

    /// Stop after a pause: 1.3 s once words have been heard, 6 s if nothing was said.
    private func armSilence(screen: String?, first: Bool = false) {
        silence?.cancel()
        let wait: UInt64 = first || partial.isEmpty ? 6_000_000_000 : 1_300_000_000
        silence = Task { [weak self] in
            try? await Task.sleep(nanoseconds: wait)
            guard !Task.isCancelled else { return }
            await MainActor.run { self?.stopListening(send: true, screen: screen) }
        }
    }

    func stopListening(send: Bool, screen: String? = nil) {
        guard listening else { return }
        listening = false
        silence?.cancel()
        engine.stop()
        engine.inputNode.removeTap(onBus: 0)
        request?.endAudio()
        task?.cancel()
        request = nil; task = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        let heard = partial
        partial = ""
        if send && !heard.isEmpty { self.send(heard, screen: screen, voice: true) }
    }

    private func speak(_ text: String) {
        guard !text.isEmpty else { return }
        try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .spokenAudio, options: [.duckOthers])
        try? AVAudioSession.sharedInstance().setActive(true)
        let u = AVSpeechUtterance(string: text)
        u.voice = AVSpeechSynthesisVoice(language: Locale.current.identifier.replacingOccurrences(of: "_", with: "-"))
            ?? AVSpeechSynthesisVoice(language: "en-US")
        u.rate = AVSpeechUtteranceDefaultSpeechRate * 1.05
        synth.speak(u)
    }
}

enum Haptics {
    @MainActor static func tap() { UIImpactFeedbackGenerator(style: .light).impactOccurred() }
}

// MARK: - floating button

/// The mic that floats above every tab. Tap opens the quick chat; press and hold opens it already listening.
struct AskButton: View {
    @Environment(Router.self) private var router
    @Environment(HomeStore.self) private var store
    private var model: AssistantModel { .shared }

    var body: some View {
        if model.ready && !router.showAssistant {
            Image(systemName: model.listening ? "waveform" : "mic.fill")
                .font(.system(size: 20, weight: .semibold))
                .foregroundStyle(Theme.onAccent)
                .frame(width: 52, height: 52)
                .background(tint, in: Circle())
                .shadow(color: .black.opacity(0.22), radius: 10, y: 4)
                .contentShape(Circle())
                .onTapGesture { router.showAssistant = true }
                .onLongPressGesture(minimumDuration: 0.35) {
                    router.showAssistant = true
                    Task { await model.startListening(screen: router.screenName) }
                }
                .accessibilityLabel("Ask Thomas Creek")
                .accessibilityHint("Double-tap to open. Touch and hold to talk.")
                .accessibilityAddTraits(.isButton)
                .padding(.trailing, 16)
                .padding(.bottom, 64)
                .transition(.scale.combined(with: .opacity))
        }
    }

    private var tint: Color { store.role == .child ? (store.me?.style == .little ? Theme.little : Theme.kid) : Theme.accent }
}

// MARK: - panel

struct AssistantSheet: View {
    @Environment(Router.self) private var router
    @Environment(HomeStore.self) private var store
    @FocusState private var typing: Bool
    @State private var model = AssistantModel.shared

    var body: some View {
        VStack(spacing: 0) {
            header
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 8) {
                        if model.lines.isEmpty {
                            bubble(.init(kind: .house, text: model.greeting(for: store.me?.person.name, kid: kid)))
                            suggestions
                        }
                        ForEach(model.lines) { bubble($0).id($0.id) }
                        ForEach(model.cards) { card($0).id($0.id) }
                        if model.busy { TypingDots().id("busy") }
                        if model.listening { listeningBubble.id("listen") }
                        Color.clear.frame(height: 1).id("end")
                    }
                    .padding(.horizontal, 14).padding(.vertical, 10)
                }
                .scrollDismissesKeyboard(.interactively)
                .onChange(of: model.lines.count) { withAnimation { proxy.scrollTo("end") } }
                .onChange(of: model.cards.count) { withAnimation { proxy.scrollTo("end") } }
                .onChange(of: model.busy) { withAnimation { proxy.scrollTo("end") } }
                .onChange(of: model.partial) { proxy.scrollTo("end") }
            }
            inputBar
        }
        .background(Theme.sheet)
        .presentationDetents([.fraction(0.46), .large])
        .presentationBackgroundInteraction(.enabled(upThrough: .fraction(0.46)))
        .presentationDragIndicator(.visible)
        .presentationCornerRadius(24)
        .onDisappear { model.stopListening(send: false) }
    }

    private var kid: Bool { store.role == .child }

    private var header: some View {
        HStack(spacing: 10) {
            Image("tc.home").resizable().scaledToFit().frame(width: 22, height: 22).foregroundStyle(Theme.accent)
            Text("Ask Thomas Creek").font(.headline)
            Spacer()
            if !model.lines.isEmpty {
                Button { model.newConversation() } label: { Image(systemName: "square.and.pencil") }
                    .accessibilityLabel("New conversation")
            }
            Button { model.speakReplies.toggle() } label: {
                Image(systemName: model.speakReplies ? "speaker.wave.2.fill" : "speaker.slash.fill")
            }
            .accessibilityLabel(model.speakReplies ? "Spoken replies on" : "Spoken replies off")
        }
        .foregroundStyle(Theme.fg)
        .padding(.horizontal, 16).padding(.top, 14).padding(.bottom, 6)
    }

    private var suggestions: some View {
        let s: [String] = kid
            ? ["Make my room lights purple", "What chores do I have left?", "Can I have more iPad time?"]
            : ["Turn off the downstairs lights", "What's for dinner?", "Was anyone at the front door today?", "Add milk to the shopping list"]
        return ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(s, id: \.self) { q in
                    Button { model.send(q, screen: router.screenName) } label: {
                        Text(q).font(.footnote).padding(.horizontal, 11).padding(.vertical, 7)
                            .background(Theme.card, in: Capsule()).overlay(Capsule().stroke(Theme.line))
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    @ViewBuilder private func bubble(_ l: AssistantModel.Line) -> some View {
        VStack(alignment: l.kind == .me ? .trailing : .leading, spacing: 4) {
            if !l.steps.isEmpty {
                FlowSteps(steps: l.steps)
            }
            Text(l.text)
                .font(kid ? Theme.kidFont(16, .medium) : .body)
                .padding(.horizontal, 12).padding(.vertical, 9)
                .foregroundStyle(l.kind == .me ? Theme.onAccent : (l.kind == .error ? Theme.warn : Theme.fg))
                .background(l.kind == .me ? AnyShapeStyle(Theme.accent) : AnyShapeStyle(Theme.card),
                            in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                .textSelection(.enabled)
        }
        .frame(maxWidth: .infinity, alignment: l.kind == .me ? .trailing : .leading)
        .padding(l.kind == .me ? .leading : .trailing, 40)
    }

    private var listeningBubble: some View {
        HStack(spacing: 8) {
            Image(systemName: "waveform").symbolEffect(.variableColor.iterative, options: .repeating)
            Text(model.partial.isEmpty ? "Listening…" : model.partial).foregroundStyle(model.partial.isEmpty ? Theme.dim : Theme.fg)
        }
        .padding(.horizontal, 12).padding(.vertical, 9)
        .background(Theme.card2, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
        .frame(maxWidth: .infinity, alignment: .trailing)
        .padding(.leading, 40)
    }

    @ViewBuilder private func card(_ c: AssistantModel.Card) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(c.confirm.summary, systemImage: c.confirm.kind == "faceid" ? "faceid" : "exclamationmark.circle")
                .font(.subheadline.weight(.semibold))
            switch c.state {
            case .waiting:
                HStack(spacing: 8) {
                    Button { model.confirm(c) } label: {
                        Text(c.confirm.kind == "faceid" ? "Confirm with Face ID" : (c.confirm.method == "DELETE" ? "Delete" : "Confirm"))
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(c.confirm.method == "DELETE" ? Theme.crit : Theme.accent)
                    Button("Cancel") { model.cancel(c) }.buttonStyle(.bordered)
                }
            case .running: ProgressView().frame(maxWidth: .infinity)
            case .done: Label("Done", systemImage: "checkmark.circle.fill").font(.footnote).foregroundStyle(Theme.accent)
            case .cancelled: Text("Cancelled").font(.footnote).foregroundStyle(Theme.dim)
            case .failed(let m): Label(m, systemImage: "exclamationmark.triangle.fill").font(.footnote).foregroundStyle(Theme.warn)
            }
        }
        .padding(12)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).stroke(Theme.warn.opacity(0.35)))
        .padding(.trailing, 40)
    }

    private var inputBar: some View {
        HStack(spacing: 8) {
            TextField(kid ? "Ask the house…" : "Ask or tell the house…", text: $model.draft, axis: .vertical)
                .lineLimit(1...4)
                .focused($typing)
                .submitLabel(.send)
                .onSubmit { model.send(model.draft, screen: router.screenName) }
                .padding(.horizontal, 14).padding(.vertical, 10)
                .background(Theme.card, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 22, style: .continuous).stroke(Theme.line))
            if !model.draft.isEmpty {
                Button { model.send(model.draft, screen: router.screenName) } label: {
                    Image(systemName: "arrow.up").font(.headline).foregroundStyle(Theme.onAccent)
                        .frame(width: 42, height: 42).background(Theme.accent, in: Circle())
                }
                .accessibilityLabel("Send")
                .disabled(model.busy)
            } else {
                Button { typing = false; model.toggleListening(screen: router.screenName) } label: {
                    Image(systemName: model.listening ? "stop.fill" : "mic.fill").font(.headline).foregroundStyle(Theme.onAccent)
                        .frame(width: 42, height: 42)
                        .background(model.listening ? Theme.crit : Theme.accent, in: Circle())
                }
                .accessibilityLabel(model.listening ? "Stop listening" : "Talk")
            }
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
    }
}

/// What the hub did, as small chips above the reply ("Turn off basement lights ✓").
private struct FlowSteps: View {
    let steps: [AskReply.Step]
    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            ForEach(steps) { s in
                Label(s.label, systemImage: icon(s.status))
                    .font(.caption)
                    .foregroundStyle(s.status == "done" ? Theme.accent : Theme.warn)
            }
        }
    }
    private func icon(_ s: String) -> String {
        switch s { case "done": "checkmark.circle.fill"; case "denied": "hand.raised.fill"; default: "exclamationmark.triangle.fill" }
    }
}

private struct TypingDots: View {
    @State private var on = false
    var body: some View {
        HStack(spacing: 4) {
            ForEach(0..<3) { i in
                Circle().fill(Theme.dim).frame(width: 6, height: 6)
                    .opacity(on ? 1 : 0.3)
                    .animation(.easeInOut(duration: 0.5).repeatForever().delay(Double(i) * 0.15), value: on)
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 12)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
        .onAppear { on = true }
        .accessibilityLabel("Working on it")
    }
}
