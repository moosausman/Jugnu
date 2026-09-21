import Foundation

struct ChatMessage: Identifiable, Equatable {
    enum Role: String {
        case user
        case assistant
        case tool
    }

    /// Where this message came from. Almost always `.reply` -- a direct
    /// answer to something the user asked. `.nudge` and `.digest` mark
    /// messages the backend sent unprompted (see proactive.py's
    /// initiative and daily_review.py's end-of-day recap): nobody sent a
    /// "send" that this is a response to. Defaulted so every existing
    /// `ChatMessage(role:content:)` call site keeps compiling unchanged;
    /// a view can opt into styling these differently later, but nothing
    /// requires it to.
    enum Origin: String {
        case reply
        case nudge
        case digest
        /// A background or scheduled workflow finished (or needs the user).
        case background
    }

    let id = UUID()
    let role: Role
    var content: String
    var origin: Origin = .reply
}
