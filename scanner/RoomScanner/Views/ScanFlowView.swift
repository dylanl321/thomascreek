import RoomPlan
import SwiftUI

/// Say which room it is -> scan -> saved package. No other questions.
struct ScanFlowView: View {
    @EnvironmentObject private var library: RoomLibrary
    let onDone: (ScanSummary?) -> Void

    @State private var text = ""
    @State private var controller: ScanController?
    @State private var saving = false
    @State private var errorText: String?
    @FocusState private var focused: Bool

    var body: some View {
        ZStack {
            if let controller {
                ScanningScreen(controller: controller, onCancel: {
                    controller.cancel()
                    onDone(nil)
                })
            } else {
                describeScreen
            }
            if saving {
                Color.black.opacity(0.55).ignoresSafeArea()
                VStack(spacing: 14) {
                    ProgressView().controlSize(.large).tint(.white)
                    Text("Building your dollhouse…").font(.headline).foregroundStyle(.white)
                }
            }
        }
        .alert("Something went wrong", isPresented: Binding(get: { errorText != nil }, set: { if !$0 { errorText = nil } })) {
            Button("OK") { onDone(nil) }
        } message: {
            Text(errorText ?? "")
        }
    }

    private var describeScreen: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 18) {
                Text("Which room is this?")
                    .font(.largeTitle.bold())
                Text("One line is enough: the room and how it connects.")
                    .foregroundStyle(.secondary)
                TextField("Guest bath, through the door on the left wall of the bedroom", text: $text, axis: .vertical)
                    .font(.title3)
                    .lineLimit(2...5)
                    .padding(14)
                    .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 14))
                    .focused($focused)
                    .submitLabel(.go)
                    .onSubmit(start)
                Label("Tap the mic on the keyboard to say it instead of typing.", systemImage: "mic")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                Spacer()
                Button(action: start) {
                    Label("Start scanning", systemImage: "viewfinder")
                        .font(.title3.weight(.semibold))
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 14)
                }
                .buttonStyle(.borderedProminent)
                .buttonBorderShape(.capsule)
            }
            .padding(24)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { onDone(nil) }
                }
            }
            .onAppear { focused = true }
        }
    }

    private func start() {
        focused = false
        let description = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let controller = ScanController(description: description)
        controller.onComplete = { output in
            saving = true
            Task { @MainActor in
                do {
                    let summary = try await PackageWriter.write(output, library: library)
                    saving = false
                    onDone(summary)
                } catch {
                    saving = false
                    errorText = error.localizedDescription
                }
            }
        }
        self.controller = controller
    }
}

private struct ScanningScreen: View {
    @ObservedObject var controller: ScanController
    let onCancel: () -> Void

    @State private var askingNext = false
    @State private var nextText = ""

    var body: some View {
        ZStack {
            RoomCaptureContainer(controller: controller)
                .ignoresSafeArea()

            VStack {
                HStack {
                    Button("Cancel", action: onCancel)
                        .buttonStyle(.bordered)
                        .tint(.white)
                    Spacer()
                    Label("\(controller.photoCount)", systemImage: "camera")
                        .font(.subheadline.monospacedDigit())
                        .padding(.horizontal, 12).padding(.vertical, 6)
                        .background(.ultraThinMaterial, in: Capsule())
                }
                .padding()
                Spacer()
                if controller.phase == .processing {
                    ProgressView("Processing…")
                        .padding()
                        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 14))
                }
                if case .failed(let message) = controller.phase {
                    Text(message)
                        .padding()
                        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 14))
                    Button("Close", action: onCancel).buttonStyle(.borderedProminent)
                }
                HStack(spacing: 14) {
                    Button {
                        nextText = ""
                        askingNext = true
                    } label: {
                        Label("Next room", systemImage: "arrow.right.square")
                            .frame(maxWidth: .infinity).padding(.vertical, 10)
                    }
                    .buttonStyle(.bordered)
                    .tint(.white)
                    Button {
                        controller.finish()
                    } label: {
                        Label("Done", systemImage: "checkmark")
                            .font(.headline)
                            .frame(maxWidth: .infinity).padding(.vertical, 10)
                    }
                    .buttonStyle(.borderedProminent)
                }
                .disabled(controller.phase != .scanning)
                .padding()
            }
        }
        .onAppear { controller.start() }
        .alert("Which room is next?", isPresented: $askingNext) {
            TextField("Kitchen, through the opening behind the couch", text: $nextText)
            Button("Scan it") { controller.nextRoom(description: nextText) }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Walk through, then keep scanning. The rooms are lined up automatically.")
        }
    }
}

private struct RoomCaptureContainer: UIViewRepresentable {
    let controller: ScanController

    func makeUIView(context: Context) -> RoomCaptureView { controller.captureView }
    func updateUIView(_ uiView: RoomCaptureView, context: Context) {}
}
