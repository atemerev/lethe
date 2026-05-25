use std::collections::BTreeMap;
use std::fmt::Write as _;

/// Small helper that assembles a system prompt from labeled sections.
///
/// Sections are emitted in insertion order, joined by blank lines. Empty
/// sections are skipped. Each section is rendered as either:
/// - a raw paragraph (`Section::Raw`), or
/// - an XML-ish block: `<tag attr="...">\n<body>\n</tag>` (`Section::Block`).
#[derive(Debug, Default)]
pub struct PromptBuilder {
    sections: Vec<Section>,
}

#[derive(Debug)]
enum Section {
    Raw(String),
    Block {
        tag: String,
        attrs: BTreeMap<String, String>,
        body: String,
    },
}

impl PromptBuilder {
    pub fn new() -> Self {
        Self::default()
    }

    /// Append a raw paragraph. Empty/whitespace-only content is skipped.
    pub fn raw(&mut self, content: impl Into<String>) -> &mut Self {
        let content = content.into();
        if !content.trim().is_empty() {
            self.sections.push(Section::Raw(content));
        }
        self
    }

    /// Append a `<tag>\n<body>\n</tag>` block. Empty body is skipped.
    pub fn block(&mut self, tag: &str, body: impl Into<String>) -> &mut Self {
        self.block_with(tag, [], body)
    }

    /// Append a `<tag attr="value" ...>\n<body>\n</tag>` block. Empty body is skipped.
    pub fn block_with<const N: usize>(
        &mut self,
        tag: &str,
        attrs: [(&str, &str); N],
        body: impl Into<String>,
    ) -> &mut Self {
        let body = body.into();
        if body.trim().is_empty() {
            return self;
        }
        let attrs = attrs
            .into_iter()
            .filter(|(_, value)| !value.is_empty())
            .map(|(key, value)| (key.to_string(), value.to_string()))
            .collect();
        self.sections.push(Section::Block {
            tag: tag.to_string(),
            attrs,
            body,
        });
        self
    }

    pub fn render(&self) -> String {
        let mut output = String::new();
        for (index, section) in self.sections.iter().enumerate() {
            if index > 0 {
                output.push_str("\n\n");
            }
            match section {
                Section::Raw(content) => output.push_str(content.trim()),
                Section::Block { tag, attrs, body } => {
                    output.push('<');
                    output.push_str(tag);
                    for (key, value) in attrs {
                        let _ = write!(output, " {key}=\"{}\"", escape_attr(value));
                    }
                    output.push('>');
                    output.push('\n');
                    output.push_str(body.trim());
                    output.push('\n');
                    output.push_str("</");
                    output.push_str(tag);
                    output.push('>');
                }
            }
        }
        output
    }
}

/// Escape XML-significant characters inside attribute values so a stray `"` or
/// `<` in a value (e.g. a timestamp with quotes, a URL with `&`) doesn't break
/// the surrounding `<tag attr="...">` rendering.
fn escape_attr(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for ch in value.chars() {
        match ch {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            other => out.push(other),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn renders_raw_and_block_sections() {
        let mut builder = PromptBuilder::new();
        builder
            .raw("You are Lethe.")
            .block("identity_block", "I help with...")
            .block_with(
                "runtime_context",
                [("source", "hippocampus"), ("timestamp", "now")],
                "recall here",
            );

        let rendered = builder.render();
        assert_eq!(
            rendered,
            "You are Lethe.\n\n<identity_block>\nI help with...\n</identity_block>\n\n<runtime_context source=\"hippocampus\" timestamp=\"now\">\nrecall here\n</runtime_context>"
        );
    }

    #[test]
    fn skips_empty_sections() {
        let mut builder = PromptBuilder::new();
        builder
            .raw("")
            .block("empty", "   ")
            .block_with("with_attr", [("a", "")], "body");
        assert_eq!(builder.render(), "<with_attr>\nbody\n</with_attr>");
    }
}
