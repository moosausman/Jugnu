import Foundation

/// A request Swift sends to the Python bridge over stdin, one JSON object per line.
///
/// `type`/`id`/`text` cover the original requests (send, clear, ping, ...).
/// Newer ones -- approval responses, cancel/pause, permission and memory
/// edits -- need more fields, which go in `extra` and are merged into the
/// same top-level object when encoded. Existing call sites that only use
/// the three original fields compile unchanged.
struct OutgoingRequest: Encodable {
    var type: String
    var id: String?
    var text: String?
    var extra: [String: JSONValue] = [:]

    init(type: String, id: String?, text: String?, extra: [String: JSONValue] = [:]) {
        self.type = type
        self.id = id
        self.text = text
        self.extra = extra
    }

    private struct Key: CodingKey {
        var stringValue: String
        var intValue: Int? { nil }
        init(_ s: String) { stringValue = s }
        init?(stringValue: String) { self.stringValue = stringValue }
        init?(intValue: Int) { nil }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: Key.self)
        try c.encode(type, forKey: Key("type"))
        if let id { try c.encode(id, forKey: Key("id")) }
        if let text { try c.encode(text, forKey: Key("text")) }
        for (k, v) in extra where !["type", "id", "text"].contains(k) {
            try c.encode(v, forKey: Key(k))
        }
    }
}

/// An event the Python bridge sends back over stdout, one JSON object per line.
/// Fields are optional because different `type`s populate different subsets --
/// see bridge.py's module docstring for the authoritative protocol reference.
///
/// `payload` holds the entire event as JSON, so richer events (plans,
/// approvals, tool activity, memories, skills, permissions) can be read with
/// JSONValue's accessors without a dedicated field for every key.
struct IncomingEvent: Decodable {
    var type: String
    var id: String?

    // "ready"
    var plannerModel: String?
    var agentModel: String?
    var toolCount: Int?
    var history: [HistoryItem]?
    var proactive: Bool?
    var proactiveActions: Bool?
    var activityTracking: Bool?
    var dailyReview: Bool?

    // "status" / "tool"
    var label: String?
    var name: String?

    // "assistant" / "nudge" / "digest" / "tool"
    var content: String?

    // "error"
    var message: String?

    var payload: JSONValue = .null

    enum CodingKeys: String, CodingKey {
        case type, id, history, label, name, content, message, proactive
        case plannerModel = "planner_model"
        case agentModel = "agent_model"
        case toolCount = "tool_count"
        case proactiveActions = "proactive_actions"
        case activityTracking = "activity_tracking"
        case dailyReview = "daily_review"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        type = try c.decode(String.self, forKey: .type)
        id = try? c.decodeIfPresent(String.self, forKey: .id)
        plannerModel = try? c.decodeIfPresent(String.self, forKey: .plannerModel)
        agentModel = try? c.decodeIfPresent(String.self, forKey: .agentModel)
        toolCount = try? c.decodeIfPresent(Int.self, forKey: .toolCount)
        history = try? c.decodeIfPresent([HistoryItem].self, forKey: .history)
        proactive = try? c.decodeIfPresent(Bool.self, forKey: .proactive)
        proactiveActions = try? c.decodeIfPresent(Bool.self, forKey: .proactiveActions)
        activityTracking = try? c.decodeIfPresent(Bool.self, forKey: .activityTracking)
        dailyReview = try? c.decodeIfPresent(Bool.self, forKey: .dailyReview)
        label = try? c.decodeIfPresent(String.self, forKey: .label)
        name = try? c.decodeIfPresent(String.self, forKey: .name)
        content = try? c.decodeIfPresent(String.self, forKey: .content)
        message = try? c.decodeIfPresent(String.self, forKey: .message)
        payload = (try? JSONValue(from: decoder)) ?? .null
    }
}

struct HistoryItem: Codable {
    var role: String
    var content: String
}
