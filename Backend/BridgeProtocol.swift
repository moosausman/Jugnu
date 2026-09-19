import Foundation

/// A request Swift sends to the Python bridge over stdin, one JSON object per line.
struct OutgoingRequest: Codable {
    var type: String
    var id: String?
    var text: String?
}

/// An event the Python bridge sends back over stdout, one JSON object per line.
/// Fields are optional because different `type`s populate different subsets --
/// see bridge.py's module docstring for the authoritative protocol reference.
///
/// Not modeled here on purpose: "plan" events (goal/tasks/depends_on) and
/// "status"/"tool"'s task_id/agent fields. bridge.py sends them, but nothing
/// on this side reads them yet -- Codable silently ignores JSON keys with no
/// matching property, so leaving them out doesn't break decoding, it just
/// means that detail isn't surfaced anywhere in the UI yet.
struct IncomingEvent: Codable {
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

    enum CodingKeys: String, CodingKey {
        case type, id, history, label, name, content, message, proactive
        case plannerModel = "planner_model"
        case agentModel = "agent_model"
        case toolCount = "tool_count"
        case proactiveActions = "proactive_actions"
        case activityTracking = "activity_tracking"
        case dailyReview = "daily_review"
    }
}

struct HistoryItem: Codable {
    var role: String
    var content: String
}
