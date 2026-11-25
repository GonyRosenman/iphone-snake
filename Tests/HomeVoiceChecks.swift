// Mutations caught: allowing plain HTTP to remote hosts, URL credentials or queries.
import Foundation
@main
struct HomeVoiceChecks {
    static func main() throws {
        for address in ["https://home.example", "https://home.example:8443", "http://127.0.0.1:8765"] {
            _ = try HomeAddress.validate(address)
        }
        for address in ["http://home.example", "https://user:secret@home.example", "https://home.example?token=secret", "https://home.example/path", "file:///etc/passwd"] {
            do { _ = try HomeAddress.validate(address); fatalError("Unsafe address accepted") }
            catch is VoiceError {}
        }
        print("PASS: home connection accepts HTTPS and rejects remote cleartext/embedded credentials")
    }
}
