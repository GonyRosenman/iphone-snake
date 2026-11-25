import Foundation

struct HomeStatus: Decodable {
    let ready: Bool
    let message: String
}
struct CallAnswer: Decodable {
    let callID: String
    let sdp: String
    let model: String
}
struct HomeEvent: Decodable {
    let type: String
    let callID: String?
    let text: String?
    let connectionID: String?
}

enum VoiceError: LocalizedError {
    case message(String)
    var errorDescription: String? { if case .message(let text) = self { return text }; return nil }
}

enum HomeAddress {
    static func validate(_ value: String) throws -> URL {
        guard let url = URL(string: value.trimmingCharacters(in: .whitespacesAndNewlines)),
              let host = url.host, !host.isEmpty, url.user == nil, url.password == nil,
              url.query == nil, url.fragment == nil, url.path.isEmpty || url.path == "/",
              url.scheme == "https" || (url.scheme == "http" && ["localhost", "127.0.0.1", "::1"].contains(host))
        else { throw VoiceError.message("הזן כתובת HTTPS של המחשב הביתי, בלי פרטי כניסה בכתובת.") }
        return url
    }
}
