import SwiftUI

/// Renders the Markdown the models actually write -- headings, lists,
/// numbered steps, code blocks, quotes, rules and simple tables -- instead of
/// showing "###" and "---" literally. Inline styling (bold, italics, `code`,
/// links) goes through Foundation's own Markdown parser.
struct MarkdownText: View {
    let source: String
    var size: CGFloat = 13.5
    var color: Color = Theme.ink

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            ForEach(Array(MarkdownBlock.parse(source).enumerated()), id: \.offset) { _, block in
                view(for: block)
            }
        }
        .tint(Theme.clay)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder
    private func view(for block: MarkdownBlock) -> some View {
        switch block {
        case .heading(let level, let text):
            Text(inline(text))
                .font(Theme.text(level == 1 ? size + 3 : (level == 2 ? size + 1.5 : size + 0.5), weight: .semibold))
                .foregroundStyle(color)
                .padding(.top, 4)
                .fixedSize(horizontal: false, vertical: true)

        case .paragraph(let text):
            Text(inline(text))
                .font(Theme.text(size))
                .lineSpacing(Theme.bodyLineSpacing)
                .foregroundStyle(color)
                .fixedSize(horizontal: false, vertical: true)

        case .list(let items):
            VStack(alignment: .leading, spacing: 4) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    HStack(alignment: .firstTextBaseline, spacing: 7) {
                        Text(item.marker)
                            .font(Theme.text(size, weight: item.marker == "•" ? .bold : .medium))
                            .foregroundStyle(Theme.inkFaint)
                            .frame(minWidth: item.marker == "•" ? 8 : 16, alignment: .trailing)
                        Text(inline(item.text))
                            .font(Theme.text(size))
                            .lineSpacing(Theme.bodyLineSpacing)
                            .foregroundStyle(color)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(.leading, CGFloat(item.depth) * 16)
                }
            }

        case .code(let text):
            ScrollView(.horizontal, showsIndicators: false) {
                Text(text)
                    .font(Theme.mono(size - 2))
                    .foregroundStyle(Theme.ink.opacity(0.9))
                    .fixedSize()
                    .padding(10)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: Theme.radiusSmall).fill(Theme.well))

        case .quote(let text):
            Text(inline(text))
                .font(Theme.text(size))
                .lineSpacing(Theme.bodyLineSpacing)
                .foregroundStyle(Theme.inkDim)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.leading, 10)
                .overlay(alignment: .leading) {
                    RoundedRectangle(cornerRadius: 1).fill(Theme.clay.opacity(0.55)).frame(width: 2)
                }

        case .rule:
            Rectangle().fill(Theme.border).frame(height: 1).padding(.vertical, 4)

        case .table(let header, let rows):
            // A table is a panel, not loose text: headers in small caps over
            // a rule, rows on a steady rhythm, figures in lining digits so
            // columns of numbers line up.
            Grid(alignment: .leading, horizontalSpacing: 18, verticalSpacing: 7) {
                GridRow {
                    ForEach(Array(header.enumerated()), id: \.offset) { _, cell in
                        Text(inline(cell).uppercasedForHeader)
                            .font(Theme.text(size - 3.5, weight: .semibold))
                            .tracking(0.6)
                            .foregroundStyle(Theme.inkFaint)
                    }
                }
                Rectangle().fill(Theme.border).frame(height: 1)
                    .gridCellUnsizedAxes(.horizontal)
                    .gridCellColumns(max(1, header.count))
                ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                    GridRow {
                        ForEach(Array(row.enumerated()), id: \.offset) { _, cell in
                            Text(inline(cell))
                                .font(Theme.text(size - 1))
                                .monospacedDigit()
                                .foregroundStyle(color)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            }
            .padding(.vertical, 9)
            .padding(.horizontal, 12)
            .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(Theme.panel))
            .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous).stroke(Theme.border, lineWidth: 1))
            .padding(.vertical, 2)
        }
    }

    private func inline(_ text: String) -> AttributedString {
        inlineMarkdown(text)
    }

    private func inlineMarkdown(_ text: String) -> AttributedString {
        (try? AttributedString(markdown: text, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)))
            ?? AttributedString(text)
    }
}

enum MarkdownBlock: Equatable {
    struct Item: Equatable { let marker: String; let text: String; let depth: Int }

    case heading(Int, String)
    case paragraph(String)
    case list([Item])
    case code(String)
    case quote(String)
    case rule
    case table([String], [[String]])

    static func parse(_ source: String) -> [MarkdownBlock] {
        var blocks: [MarkdownBlock] = []
        var paragraph: [String] = []
        var items: [Item] = []
        var quote: [String] = []
        var code: [String]? = nil
        var table: [[String]] = []

        func flush() {
            if !paragraph.isEmpty { blocks.append(.paragraph(paragraph.joined(separator: "\n"))); paragraph = [] }
            if !items.isEmpty { blocks.append(.list(items)); items = [] }
            if !quote.isEmpty { blocks.append(.quote(quote.joined(separator: "\n"))); quote = [] }
            if !table.isEmpty {
                if table.count >= 2 {
                    let width = table[0].count
                    let rows = table.dropFirst().map { row in
                        Array((row + Array(repeating: "", count: max(0, width - row.count))).prefix(width))
                    }
                    blocks.append(.table(table[0], rows))
                } else {
                    blocks.append(.paragraph(table[0].joined(separator: "  ")))
                }
                table = []
            }
        }

        for raw in source.replacingOccurrences(of: "\r\n", with: "\n").components(separatedBy: "\n") {
            let line = raw.trimmingCharacters(in: .whitespaces)

            if var open = code {
                if line.hasPrefix("```") {
                    blocks.append(.code(open.joined(separator: "\n")))
                    code = nil
                } else {
                    open.append(raw)
                    code = open
                }
                continue
            }
            if line.hasPrefix("```") { flush(); code = []; continue }
            if line.isEmpty { flush(); continue }

            if line.range(of: #"^(\*\s*\*\s*\*[\s*]*|-\s*-\s*-[\s-]*|_\s*_\s*_[\s_]*)$"#, options: .regularExpression) != nil {
                flush(); blocks.append(.rule); continue
            }
            if let m = line.range(of: #"^#{1,6}\s+"#, options: .regularExpression) {
                flush()
                let level = line[m].filter { $0 == "#" }.count
                blocks.append(.heading(level, String(line[m.upperBound...]).trimmingCharacters(in: CharacterSet(charactersIn: "# "))))
                continue
            }
            if line.hasPrefix("|") && line.hasSuffix("|") && line.count > 2 {
                if !paragraph.isEmpty || !items.isEmpty || !quote.isEmpty {
                    let pending = table; flush(); table = pending
                }
                let cells = line.dropFirst().dropLast().split(separator: "|", omittingEmptySubsequences: false)
                    .map { $0.trimmingCharacters(in: .whitespaces) }
                if cells.allSatisfy({ $0.range(of: #"^:?-{2,}:?$"#, options: .regularExpression) != nil }) { continue }
                table.append(cells)
                continue
            }
            if !table.isEmpty { flush() }
            if line.hasPrefix(">") {
                if !paragraph.isEmpty || !items.isEmpty { flush() }
                quote.append(String(line.dropFirst()).trimmingCharacters(in: .whitespaces))
                continue
            }
            let indent = raw.prefix(while: { $0 == " " || $0 == "\t" })
                .reduce(0) { $0 + ($1 == "\t" ? 4 : 1) }
            if let m = line.range(of: #"^[-*+•]\s+"#, options: .regularExpression) {
                if !paragraph.isEmpty || !quote.isEmpty { let pending = items; flush(); items = pending }
                items.append(Item(marker: "•", text: String(line[m.upperBound...]), depth: min(indent / 2, 3)))
                continue
            }
            if let m = line.range(of: #"^\d{1,3}[.)]\s+"#, options: .regularExpression) {
                if !paragraph.isEmpty || !quote.isEmpty { let pending = items; flush(); items = pending }
                let number = line[m].trimmingCharacters(in: .whitespaces).dropLast()
                items.append(Item(marker: "\(number).", text: String(line[m.upperBound...]), depth: min(indent / 2, 3)))
                continue
            }
            if !items.isEmpty && indent >= 2, let last = items.popLast() {
                // A wrapped continuation of the previous list item.
                items.append(Item(marker: last.marker, text: last.text + "\n" + line, depth: last.depth))
                continue
            }
            if !items.isEmpty || !quote.isEmpty { flush() }
            paragraph.append(line)
        }
        if let open = code { blocks.append(.code(open.joined(separator: "\n"))) }
        flush()
        return blocks
    }
}

#Preview {
    ScrollView {
        MarkdownText(source: """
        ### What the day looked like
        Screen time was **low** and concentrated in `Safari`.

        ---

        1. **Settle the search.** Build a tracking doc.
           Keep it short.
        2. Clear the overdue tutorial.
        - one
          - nested
        > A quote

        | App | Time |
        |---|---|
        | Safari | 2h |
        ```
        let x = 1
        ```
        """)
        .padding()
    }
    .frame(width: 380, height: 600)
    .background(Theme.windowBackground)
}


extension AttributedString {
    /// Header text for a table: the wording the model wrote, set as a label.
    var uppercasedForHeader: AttributedString {
        var copy = self
        copy.characters.replaceSubrange(copy.startIndex..<copy.endIndex,
                                        with: String(copy.characters).uppercased())
        return copy
    }
}
