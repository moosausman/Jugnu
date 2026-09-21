import Foundation

// MARK: - JSON

/// A fully general JSON value. The backend's newer events (plans, approvals,
/// tool activity, memories, skills, permissions) carry nested structures the
/// flat `IncomingEvent` fields can't express; this lets them travel intact
/// and be read with typed accessors below, without a protocol type per event.
enum JSONValue: Codable, Equatable, Hashable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let n = try? c.decode(Double.self) { self = .number(n) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([JSONValue].self) { self = .array(a) }
        else if let o = try? c.decode([String: JSONValue].self) { self = .object(o) }
        else { throw DecodingError.dataCorruptedError(in: c, debugDescription: "Unsupported JSON value") }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let s): try c.encode(s)
        case .number(let n): try c.encode(n)
        case .bool(let b): try c.encode(b)
        case .object(let o): try c.encode(o)
        case .array(let a): try c.encode(a)
        case .null: try c.encodeNil()
        }
    }

    subscript(key: String) -> JSONValue? {
        if case .object(let o) = self { return o[key] }
        return nil
    }

    var string: String? {
        switch self {
        case .string(let s): return s
        case .number(let n): return n.rounded() == n ? String(Int(n)) : String(n)
        case .bool(let b): return b ? "true" : "false"
        default: return nil
        }
    }
    var double: Double? {
        switch self {
        case .number(let n): return n
        case .string(let s): return Double(s)
        default: return nil
        }
    }
    var int: Int? { double.map { Int($0) } }
    var bool: Bool? {
        if case .bool(let b) = self { return b }
        if case .number(let n) = self { return n != 0 }
        return nil
    }
    var array: [JSONValue] { if case .array(let a) = self { return a }; return [] }
    var object: [String: JSONValue] { if case .object(let o) = self { return o }; return [:] }
    var isNull: Bool { if case .null = self { return true }; return false }

    /// Compact text for display (e.g. a nested detail blob in the log).
    var compactText: String {
        guard let data = try? JSONEncoder().encode(self) else { return "" }
        return String(data: data, encoding: .utf8) ?? ""
    }

    static func from(_ any: Any?) -> JSONValue {
        switch any {
        case nil: return .null
        case let v as JSONValue: return v
        case let v as String: return .string(v)
        case let v as Bool: return .bool(v)
        case let v as Int: return .number(Double(v))
        case let v as Double: return .number(v)
        case let v as [Any]: return .array(v.map { JSONValue.from($0) })
        case let v as [String: Any]: return .object(v.mapValues { JSONValue.from($0) })
        default: return .string(String(describing: any!))
        }
    }
}

// MARK: - Runs, tasks, tools

enum TaskState: String {
    case pending, running, waitingApproval = "waiting_approval", verifying, done, failed, skipped, cancelled, blocked

    var label: String {
        switch self {
        case .pending: return "queued"
        case .running: return "working"
        case .waitingApproval: return "needs you"
        case .verifying: return "checking"
        case .done: return "done"
        case .failed: return "failed"
        case .skipped: return "skipped"
        case .cancelled: return "stopped"
        case .blocked: return "blocked"
        }
    }
    var symbol: String {
        switch self {
        case .pending: return "circle"
        case .running: return "circle.dotted"
        case .waitingApproval: return "hand.raised.fill"
        case .verifying: return "checkmark.circle.badge.questionmark"
        case .done: return "checkmark.circle.fill"
        case .failed: return "xmark.circle.fill"
        case .skipped: return "arrow.uturn.forward.circle"
        case .cancelled: return "stop.circle"
        case .blocked: return "exclamationmark.circle"
        }
    }
    var isFinished: Bool { [.done, .failed, .skipped, .cancelled].contains(self) }
}

struct PlanTask: Identifiable, Equatable {
    let id: String
    var agent: String
    var description: String
    var dependsOn: [String]
    var state: TaskState = .pending
    var risk: String = ""
    var completionCriteria: String = ""
    var note: String = ""

    init(json: JSONValue) {
        id = json["id"]?.string ?? UUID().uuidString
        agent = json["agent"]?.string ?? ""
        description = json["description"]?.string ?? ""
        dependsOn = (json["depends_on"]?.array ?? []).compactMap { $0.string }
        state = TaskState(rawValue: json["status"]?.string ?? "") ?? .pending
        risk = json["risk"]?.string ?? ""
        completionCriteria = json["completion_criteria"]?.string ?? ""
    }
}

struct ToolActivity: Identifiable, Equatable {
    let id = UUID()
    let runId: String
    let taskId: String
    let tool: String
    var summary: String
    var risk: String
    var ok: Bool?
    var errorType: String?
    var preview: String = ""
    var duration: Double?
    let started = Date()
}

struct RunActivity: Identifiable, Equatable {
    let id: String              // run_id
    var requestId: String
    var goal: String
    var objective: String = ""
    var assumptions: [String] = []
    var tasks: [PlanTask] = []
    var tools: [ToolActivity] = []
    var progress: [String] = []
    var state: String = "running"      // running | paused | cancelling | done | failed | cancelled
    var origin: String = "user"
    var session: String = ""
    let started = Date()
    var finished: Date?

    var completedTasks: Int { tasks.filter { $0.state == .done }.count }
    var isActive: Bool { finished == nil }
}

// MARK: - Approvals

struct ApprovalRequest: Identifiable, Equatable {
    let id: String              // approval_id
    let runId: String
    let tool: String
    let summary: String
    let risk: String
    let capability: String
    let reversible: Bool
    let reasons: [String]
    let why: String
    let allowTaskScope: Bool
    let received = Date()

    init?(json: JSONValue) {
        guard let id = json["approval_id"]?.string else { return nil }
        self.id = id
        runId = json["run_id"]?.string ?? ""
        tool = json["tool"]?.string ?? ""
        summary = json["summary"]?.string ?? ""
        risk = json["risk"]?.string ?? "medium"
        capability = json["capability"]?.string ?? ""
        reversible = json["reversible"]?.bool ?? false
        reasons = (json["reasons"]?.array ?? []).compactMap { $0.string }
        why = json["why"]?.string ?? ""
        allowTaskScope = json["allow_task_scope"]?.bool ?? false
    }
}
