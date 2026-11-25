import SwiftUI
import AVFoundation
import Security
import WebRTC

@main
struct HomeVoiceApp: App {
    var body: some Scene { WindowGroup { VoiceView() } }
}

enum PairingSecret {
    private static let service = "org.homevoicedemo.pairing"
    static func read() -> String {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service, kSecReturnData as String: true]
        var value: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &value) == errSecSuccess,
              let data = value as? Data else { return "" }
        return String(data: data, encoding: .utf8) ?? ""
    }
    static func save(_ value: String) throws {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service]
        let attributes: [String: Any] = [kSecValueData as String: Data(value.utf8),
            kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly]
        var result = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if result == errSecItemNotFound {
            result = SecItemAdd(query.merging(attributes) { _, new in new } as CFDictionary, nil)
        }
        guard result == errSecSuccess else { throw VoiceError.message("לא ניתן לשמור את קוד החיבור באופן מאובטח.") }
    }
}

final class VoicePeer: NSObject, RTCPeerConnectionDelegate, RTCDataChannelDelegate {
    private static let initialized: Void = { RTCInitializeSSL() }()
    private let factory: RTCPeerConnectionFactory
    private var peer: RTCPeerConnection?
    private var channel: RTCDataChannel?
    private var track: RTCAudioTrack?
    var onEvent: (([String: Any]) -> Void)?
    var onFailure: (() -> Void)?
    var onConnected: ((Bool) -> Void)?
    override init() {
        _ = Self.initialized
        factory = RTCPeerConnectionFactory()
        super.init()
    }
    func offer() async throws -> String {
        let config = RTCConfiguration()
        config.sdpSemantics = .unifiedPlan
        let constraints = RTCMediaConstraints(mandatoryConstraints: nil, optionalConstraints: nil)
        guard let connection = factory.peerConnection(with: config, constraints: constraints, delegate: self)
        else { throw VoiceError.message("לא ניתן להכין חיבור קולי.") }
        peer = connection
        let source = factory.audioSource(with: RTCMediaConstraints(mandatoryConstraints: nil,
            optionalConstraints: ["googEchoCancellation": "true", "googNoiseSuppression": "true"]))
        let audio = factory.audioTrack(with: source, trackId: "voice")
        track = audio
        connection.add(audio, streamIds: ["homevoice"])
        channel = connection.dataChannel(forLabel: "oai-events", configuration: RTCDataChannelConfiguration())
        channel?.delegate = self
        let offer: RTCSessionDescription = try await withCheckedThrowingContinuation { continuation in
            connection.offer(for: RTCMediaConstraints(mandatoryConstraints: ["OfferToReceiveAudio": "true"], optionalConstraints: nil)) { description, error in
                if let error = error { continuation.resume(throwing: error) }
                else if let description = description { continuation.resume(returning: description) }
                else { continuation.resume(throwing: VoiceError.message("לא התקבלה הצעת חיבור.")) }
            }
        }
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            connection.setLocalDescription(offer) { error in
                if let error = error { continuation.resume(throwing: error) }
                else { continuation.resume() }
            }
        }
        let deadline = Date().addingTimeInterval(8)
        while connection.iceGatheringState != .complete && Date() < deadline {
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        guard connection.iceGatheringState == .complete, let sdp = connection.localDescription?.sdp
        else { throw VoiceError.message("הכנת החיבור התארכה. נסה שוב לאחר בדיקת הרשת.") }
        return sdp
    }
    func answer(_ sdp: String) async throws {
        guard let peer = peer else { throw VoiceError.message("חיבור קולי חסר.") }
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            peer.setRemoteDescription(RTCSessionDescription(type: .answer, sdp: sdp)) { error in
                if let error = error { continuation.resume(throwing: error) }
                else { continuation.resume() }
            }
        }
    }
    func mute(_ value: Bool) { track?.isEnabled = !value }
    func close() {
        track?.isEnabled = false
        channel?.close(); peer?.close()
        channel = nil; peer = nil; track = nil
    }
    func peerConnection(_ peerConnection: RTCPeerConnection, didChange stateChanged: RTCSignalingState) {}
    func peerConnection(_ peerConnection: RTCPeerConnection, didAdd stream: RTCMediaStream) {}
    func peerConnection(_ peerConnection: RTCPeerConnection, didRemove stream: RTCMediaStream) {}
    func peerConnectionShouldNegotiate(_ peerConnection: RTCPeerConnection) {}
    func peerConnection(_ peerConnection: RTCPeerConnection, didChange newState: RTCIceConnectionState) {
        onConnected?(newState == .connected || newState == .completed)
        if newState == .failed { onFailure?() }
    }
    func peerConnection(_ peerConnection: RTCPeerConnection, didChange newState: RTCIceGatheringState) {}
    func peerConnection(_ peerConnection: RTCPeerConnection, didGenerate candidate: RTCIceCandidate) {}
    func peerConnection(_ peerConnection: RTCPeerConnection, didRemove candidates: [RTCIceCandidate]) {}
    func peerConnection(_ peerConnection: RTCPeerConnection, didOpen dataChannel: RTCDataChannel) {
        // The locally-created oai-events channel owns application events.
    }
    func dataChannelDidChangeState(_ dataChannel: RTCDataChannel) {}
    func dataChannel(_ dataChannel: RTCDataChannel, didReceiveMessageWith buffer: RTCDataBuffer) {
        guard let event = try? JSONSerialization.jsonObject(with: buffer.data) as? [String: Any] else { return }
        onEvent?(event)
    }
}

@MainActor
final class VoiceModel: ObservableObject {
    @Published var home = UserDefaults.standard.string(forKey: "homevoice.address") ?? ""
    @Published var secret = PairingSecret.read()
    @Published var message = "חבר את האפליקציה למחשב בבית"
    @Published var homeConnected = false
    @Published var ready = false
    @Published var active = false
    @Published var mediaConnected = false
    @Published var busy = false
    @Published var muted = false
    @Published var finishing = false
    @Published var userCaption = ""
    @Published var agentCaption = ""
    @Published var agentWorking = false
    @Published var showSettings = false
    private var peer: VoicePeer?
    private var callID: String?
    private var controlID: String?
    private var socket: URLSessionWebSocketTask?
    private var socketReader: Task<Void, Never>?
    private var maxDuration: Task<Void, Never>?
    private var epoch = UUID()
    private let session = URLSession(configuration: .ephemeral)
    var demo: Bool { ProcessInfo.processInfo.arguments.contains("--demo") }

    init() {
        if demo {
            message = "תצוגה מקדימה · ללא חיבור או הקלטה"
            userCaption = "בדוק בבקשה את מצב העבודה בבית"
            agentCaption = "הסוכן בבית יקבל את הבקשה ויחזיר תשובה לשיחה."
        }
    }
    private func request(_ path: String, body: [String: Any]? = nil) throws -> URLRequest {
        let base = try HomeAddress.validate(home)
        guard secret.count >= 32 else { throw VoiceError.message("נדרש קוד חיבור אישי מהמחשב.") }
        var req = URLRequest(url: base.appendingPathComponent(path))
        req.timeoutInterval = 45
        req.setValue("Bearer \(secret)", forHTTPHeaderField: "Authorization")
        if let body = body {
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        return req
    }
    private func fetch<T: Decodable>(_ path: String, body: [String: Any]? = nil) async throws -> T {
        let (data, response) = try await session.data(for: request(path, body: body))
        guard let http = response as? HTTPURLResponse else { throw VoiceError.message("המחשב לא החזיר תשובה.") }
        guard (200..<300).contains(http.statusCode) else {
            switch http.statusCode {
            case 401: throw VoiceError.message("קוד החיבור אינו תקף.")
            case 409: throw VoiceError.message("השיחה בבית כבר בשימוש. לא נפתחה שיחה מתחרה.")
            case 503: throw VoiceError.message("המחשב מחובר, אך השירות הקולי עדיין אינו זמין.")
            default: throw VoiceError.message("החיבור לא הושלם. יש לבדוק את מצבו במחשב.")
            }
        }
        return try JSONDecoder().decode(T.self, from: data)
    }
    func connectHome() async {
        guard !busy, !active, !demo else { return }
        busy = true
        defer { busy = false }
        do {
            let status: HomeStatus = try await fetch("v1/status")
            try PairingSecret.save(secret)
            UserDefaults.standard.set(home, forKey: "homevoice.address")
            homeConnected = false; controlID = nil; ready = status.ready; message = status.message
            try openControl()
            showSettings = false
        } catch { homeConnected = false; ready = false; message = error.localizedDescription }
    }
    private func openControl() throws {
        socketReader?.cancel(); socket?.cancel(with: .goingAway, reason: nil)
        var req = try request("v1/events")
        var components = URLComponents(url: req.url!, resolvingAgainstBaseURL: false)!
        components.scheme = components.scheme == "https" ? "wss" : "ws"
        req.url = components.url
        let ws = session.webSocketTask(with: req)
        socket = ws; ws.resume()
        socketReader = Task { [weak self] in
            do {
                while !Task.isCancelled {
                    let frame = try await ws.receive()
                    let data: Data
                    switch frame { case .string(let text): data = Data(text.utf8); case .data(let bytes): data = bytes; @unknown default: continue }
                    if let event = try? JSONDecoder().decode(HomeEvent.self, from: data) { self?.control(event) }
                }
            } catch {
                guard !Task.isCancelled else { return }
                self?.homeConnected = false; self?.ready = false; self?.controlID = nil
                if self?.active == true { await self?.stop() }
                self?.message = "החיבור לבית נותק. חבר מחדש כדי להמשיך."
            }
        }
    }
    private func control(_ event: HomeEvent) {
        if event.type == "home.connected" {
            guard let identifier = event.connectionID else { return }
            controlID = identifier; homeConnected = true
            if ready { message = "המחשב מוכן לשיחה" }; return
        }
        guard event.callID == nil || event.callID == callID else { return }
        if event.type == "call.closed" { cleanup(); message = "השיחה הסתיימה" }
        if event.type == "call.error" { cleanup(); ready = false; message = event.text ?? "השיחה נעצרה במחשב." }
        if event.type == "agent.working" { agentWorking = true }
        if event.type == "agent.done" { agentWorking = false }
    }
    func start() async {
        guard homeConnected, ready, let controlID = controlID, !busy, !active, !demo else { return }
        busy = true; let attempt = UUID(); epoch = attempt
        message = "מכין שיחה…"
        defer { busy = false }
        do {
            let allowed = await AVCaptureDevice.requestAccess(for: .audio)
            guard allowed else { throw VoiceError.message("כדי לדבר, אפשר גישה למיקרופון בהגדרות האייפון.") }
            guard epoch == attempt else { return }
            let audio = AVAudioSession.sharedInstance()
            try audio.setCategory(.playAndRecord, mode: .voiceChat, options: [.defaultToSpeaker, .allowBluetooth])
            try audio.setActive(true)
            let rtc = VoicePeer(); peer = rtc
            rtc.onEvent = { [weak self] event in Task { @MainActor in
                guard let self = self, self.epoch == attempt else { return }; self.liveEvent(event)
            } }
            rtc.onFailure = { [weak self] in Task { @MainActor in
                guard let self = self, self.epoch == attempt else { return }; await self.stop(); self.message = "החיבור הקולי נותק."
            } }
            rtc.onConnected = { [weak self] connected in Task { @MainActor in
                guard let self = self, self.epoch == attempt else { return }
                self.mediaConnected = connected
                if connected { self.message = "אפשר לדבר" }
            } }
            let sdp = try await rtc.offer()
            guard epoch == attempt else { rtc.close(); return }
            callID = attempt.uuidString
            let answer: CallAnswer = try await fetch("v1/calls", body: ["callID": attempt.uuidString, "sdp": sdp, "controlID": controlID])
            guard epoch == attempt else { return }
            guard answer.callID == attempt.uuidString, answer.model == "gpt-live-1.1-codex" else {
                throw VoiceError.message("המחשב החזיר חיבור קולי לא צפוי.")
            }
            try await rtc.answer(answer.sdp)
            active = true; finishing = false; muted = false; userCaption = ""; agentCaption = ""
            message = "מתחבר לשיחה…"
            maxDuration = Task { [weak self] in
                try? await Task.sleep(nanoseconds: 300_000_000_000)
                if !Task.isCancelled { await self?.stop() }
            }
        } catch {
            // A failed HTTP exchange may still have admitted native voice. Reconcile this exact call.
            if callID != nil { await stop() } else { cleanup() }
            message = error.localizedDescription
        }
    }
    private func liveEvent(_ event: [String: Any]) {
        let type = event["type"] as? String ?? ""
        if type == "session.started" { message = "אפשר לדבר" }
        if type == "session.input_transcript.delta", let text = event["delta"] as? String {
            userCaption = String((userCaption + text).suffix(4000))
        }
        if type == "session.output_transcript.delta", let text = event["delta"] as? String {
            agentCaption = String((agentCaption + text).suffix(4000))
        }
        // Native Codex owns handoffs and their return path. Never dispatch a second backend task here.
    }
    func toggleMute() { muted.toggle(); peer?.mute(muted) }
    func stop() async {
        guard !finishing else { return }
        finishing = true; peer?.mute(true); message = "מסיים שיחה…"
        if let id = callID {
            struct Result: Decodable { let closed: Bool }
            do {
                let result: Result = try await fetch("v1/calls/close", body: ["callID": id])
                cleanup()
                message = result.closed ? "השיחה הסתיימה" : "סגירת השיחה עדיין לא אושרה במחשב."
            } catch { cleanup(); ready = false; message = "לא התקבל אישור סגירה. בדוק במחשב לפני שיחה נוספת." }
        } else { cleanup() }
    }
    private func cleanup() {
        epoch = UUID(); maxDuration?.cancel(); maxDuration = nil
        peer?.close(); peer = nil; callID = nil
        active = false; mediaConnected = false; muted = false; finishing = false; agentWorking = false
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }
}

struct VoiceView: View {
    @StateObject private var model = VoiceModel()
    @Environment(\.scenePhase) private var phase
    var body: some View {
        VStack(spacing: 24) {
            HStack {
                Label("בבית", systemImage: "house.fill").font(.title2.bold())
                Spacer()
                Button { model.showSettings = true } label: { Image(systemName: "gearshape") }
                    .accessibilityLabel("הגדרות חיבור")
            }
            HStack(spacing: 8) {
                Circle().fill(model.homeConnected ? Color.green : Color.secondary).frame(width: 8, height: 8)
                Text(model.homeConnected ? "מחובר למחשב בבית" : "המחשב עדיין לא מחובר").font(.subheadline)
                Spacer()
            }
            Spacer(minLength: 0)
            ZStack {
                Circle().fill(Color.cyan.opacity(0.10)).frame(width: 220, height: 220)
                Circle().stroke(Color.cyan.opacity(0.3), lineWidth: 1).frame(width: 190, height: 190)
                Image(systemName: model.muted ? "mic.slash.fill" : "waveform")
                    .font(.system(size: 68, weight: .light)).foregroundStyle(.cyan)
            }
            Text(model.active ? (model.mediaConnected ? "אני מקשיב" : "מתחבר לשיחה") : "הסוכן שלך, בקול").font(.title.bold())
            Text(model.message).font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
            if !model.userCaption.isEmpty || !model.agentCaption.isEmpty {
                ScrollView {
                    VStack(alignment: .leading, spacing: 14) {
                        if !model.userCaption.isEmpty { Text(model.userCaption).foregroundStyle(.secondary) }
                        if !model.agentCaption.isEmpty { Text(model.agentCaption) }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }.frame(maxHeight: 170)
            }
            if model.agentWorking { Label("הסוכן בבית עובד על הבקשה", systemImage: "desktopcomputer").font(.caption) }
            Spacer(minLength: 0)
            if model.active {
                HStack(spacing: 24) {
                    Button { model.toggleMute() } label: { Label(model.muted ? "בטל השתקה" : "השתק", systemImage: model.muted ? "mic.slash" : "mic") }
                        .buttonStyle(.bordered)
                    Button(role: .destructive) { Task { await model.stop() } } label: { Label("סיים", systemImage: "phone.down.fill") }
                        .buttonStyle(.borderedProminent).disabled(model.finishing)
                }
            } else {
                Button {
                    if !model.homeConnected { model.showSettings = true }
                    else { Task { await model.start() } }
                } label: {
                    Label(model.homeConnected ? "התחל שיחה" : "חבר למחשב בבית", systemImage: model.homeConnected ? "mic.fill" : "link")
                        .frame(maxWidth: .infinity).padding(.vertical, 12)
                }
                .buttonStyle(.borderedProminent).tint(.cyan).foregroundStyle(.black)
                .disabled(model.busy || (model.homeConnected && !model.ready) || model.demo)
            }
            Text("האודיו ישירות ל־OpenAI · הביצוע אצל הסוכן בבית")
                .font(.caption2).foregroundStyle(.secondary)
        }
        .padding(28).background(Color(white: 0.045)).preferredColorScheme(.dark)
        .environment(\.layoutDirection, .rightToLeft)
        .sheet(isPresented: $model.showSettings) {
            NavigationStack {
                Form {
                    Section("המחשב הביתי") {
                        TextField("https://home.example", text: $model.home)
                            .textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
                        SecureField("קוד החיבור האישי", text: $model.secret).textInputAutocapitalization(.never)
                        Text("קוד אישי מהשירות בבית. אין צורך במפתח OpenAI באייפון.").font(.caption)
                    }
                    Button("בדוק ושמור חיבור") { Task { await model.connectHome() } }.disabled(model.busy)
                    Text(model.message).font(.caption).foregroundStyle(.secondary)
                }.navigationTitle("חיבור לבית")
                    .toolbar { Button("סגור") { model.showSettings = false } }
            }.environment(\.layoutDirection, .rightToLeft)
        }
        .onChange(of: phase) { value in
            if value != .active && (model.active || model.busy) { Task { await model.stop() } }
        }
    }
}
