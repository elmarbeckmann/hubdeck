import Foundation
import Network
import Security

/// Newline-delimited JSON server on 127.0.0.1 with a random port. The token file
/// ~/Library/Application Support/HomeDeck/bridge.json (mode 0600) is the only way in:
/// this server controls locks. A connection must send {"op":"hello","token":...} first.
final class Server {
    private final class Client {
        let conn: NWConnection
        var buffer = Data()
        var authed = false
        var closed = false
        init(_ conn: NWConnection) { self.conn = conn }
    }

    private static let maxLine = 1 << 20
    private var listener: NWListener?
    private var clients: [ObjectIdentifier: Client] = [:]
    private let token: String
    /// Called on the main queue for each op of an authenticated client.
    var handler: (([String: Any], @escaping ([String: Any]) -> Void) -> Void)?

    init() {
        var bytes = [UInt8](repeating: 0, count: 32)
        guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else {
            fatalError("no random bytes")
        }
        token = bytes.map { String(format: "%02x", $0) }.joined()
    }

    func start() {
        do {
            let params = NWParameters.tcp
            params.requiredLocalEndpoint = .hostPort(host: .ipv4(.loopback), port: .any)
            let listener = try NWListener(using: params)
            listener.stateUpdateHandler = { [weak self, weak listener] state in
                switch state {
                case .ready:
                    if let port = listener?.port?.rawValue { self?.writeInfo(port: port) }
                case .failed(let error):
                    NSLog("HomeDeck listener failed: \(error)")
                    exit(1)
                default:
                    break
                }
            }
            listener.newConnectionHandler = { [weak self] conn in self?.accept(conn) }
            listener.start(queue: .main)
            self.listener = listener
        } catch {
            NSLog("HomeDeck cannot listen: \(error)")
            exit(1)
        }
    }

    func broadcast(_ message: [String: Any]) {
        for client in clients.values where client.authed { send(client, message) }
    }

    private func writeInfo(port: UInt16) {
        let dir = URL(fileURLWithPath: NSHomeDirectory())  // no sandbox: the real home
            .appendingPathComponent("Library/Application Support/HomeDeck")
        let file = dir.appendingPathComponent("bridge.json")
        do {
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
            try? FileManager.default.removeItem(at: file)  // a fresh file gets mode 0600 at creation
            let data = try JSONSerialization.data(withJSONObject: ["port": Int(port), "token": token])
            guard FileManager.default.createFile(atPath: file.path, contents: data,
                                                 attributes: [.posixPermissions: 0o600]) else {
                throw CocoaError(.fileWriteUnknown)
            }
            NSLog("HomeDeck bridge listening on 127.0.0.1:\(port)")
        } catch {
            NSLog("HomeDeck cannot write bridge.json: \(error)")
            exit(1)
        }
    }

    private func accept(_ conn: NWConnection) {
        // Belt and braces: the listener binds to loopback, refuse anything else anyway.
        if case .hostPort(let host, _) = conn.endpoint {
            switch host {
            case .ipv4(let a) where a.isLoopback: break
            case .ipv6(let a) where a.isLoopback: break
            default: conn.cancel(); return
            }
        }
        let client = Client(conn)
        clients[ObjectIdentifier(client)] = client
        conn.start(queue: .main)
        receive(client)
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) { [weak self] in
            if !client.authed { self?.drop(client) }
        }
    }

    private func receive(_ client: Client) {
        client.conn.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] data, _, done, error in
            guard let self, !client.closed else { return }
            if let data { client.buffer.append(data); self.drain(client) }
            if done || error != nil || client.closed || client.buffer.count > Server.maxLine {
                self.drop(client)
                return
            }
            self.receive(client)
        }
    }

    private func drain(_ client: Client) {
        while !client.closed, let nl = client.buffer.firstIndex(of: 10) {
            let line = Data(client.buffer[client.buffer.startIndex..<nl])
            client.buffer = Data(client.buffer[(nl + 1)...])
            guard let obj = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any] else {
                drop(client)
                return
            }
            process(client, obj)
        }
    }

    private func process(_ client: Client, _ obj: [String: Any]) {
        func reply(_ message: [String: Any]) { send(client, message) }
        if !client.authed {
            guard obj["op"] as? String == "hello", let given = obj["token"] as? String,
                  Server.equal(given, token) else {
                drop(client)
                return
            }
            client.authed = true
            var ok: [String: Any] = ["type": "result", "ok": true]
            if let req = obj["req"] { ok["req"] = req }
            reply(ok)
            return
        }
        handler?(obj, reply)
    }

    private func send(_ client: Client, _ message: [String: Any]) {
        guard !client.closed, JSONSerialization.isValidJSONObject(message),
              var data = try? JSONSerialization.data(withJSONObject: message) else { return }
        data.append(10)
        client.conn.send(content: data, completion: .contentProcessed { _ in })
    }

    private func drop(_ client: Client) {
        guard !client.closed else { return }
        client.closed = true
        client.conn.cancel()
        clients[ObjectIdentifier(client)] = nil
    }

    /// Constant-time string compare.
    private static func equal(_ a: String, _ b: String) -> Bool {
        let x = Array(a.utf8), y = Array(b.utf8)
        guard x.count == y.count else { return false }
        var diff: UInt8 = 0
        for i in 0..<x.count { diff |= x[i] ^ y[i] }
        return diff == 0
    }
}
