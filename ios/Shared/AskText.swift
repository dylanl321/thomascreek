import Foundation

/// The assistant writes light Markdown (**bold**, bullets, the odd heading). The chat shows it styled;
/// speech and Siri get the plain words, without the marks.
enum AskText {
    /// Styled text for a chat bubble. Bullets and headings become plain lines first, because the
    /// inline-only parser keeps line breaks but doesn't understand block syntax.
    static func rich(_ s: String) -> AttributedString {
        let opts = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return (try? AttributedString(markdown: blocks(s), options: opts)) ?? AttributedString(s)
    }

    /// The same words with no Markdown marks, for speaking aloud.
    static func plain(_ s: String) -> String {
        String(rich(s).characters)
    }

    /// "- item" / "* item" → "• item"; "## Title" → "**Title**". Everything else is left alone.
    static func blocks(_ s: String) -> String {
        s.components(separatedBy: "\n").map { line -> String in
            let indent = String(line.prefix(while: { $0 == " " }))
            let body = line.dropFirst(indent.count)
            if body.hasPrefix("#") {
                let title = body.drop(while: { $0 == "#" }).trimmingCharacters(in: .whitespaces)
                return title.isEmpty ? "" : indent + "**" + title.replacingOccurrences(of: "**", with: "") + "**"
            }
            for bullet in ["- ", "* ", "+ "] where body.hasPrefix(bullet) {
                return indent + "• " + body.dropFirst(2)
            }
            return line
        }.joined(separator: "\n")
    }
}
