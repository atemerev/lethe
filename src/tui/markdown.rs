//! Minimal markdown → ratatui Text converter. Handles headings, emphasis,
//! inline + fenced code, lists, and links. Designed to fit on a transcript
//! line without ANSI surprises; intentionally not a full CommonMark
//! renderer.

use pulldown_cmark::{Event, HeadingLevel, Options, Parser, Tag, TagEnd};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span, Text};

pub fn render(input: &str, width: u16) -> Text<'static> {
    let mut options = Options::empty();
    options.insert(Options::ENABLE_STRIKETHROUGH);
    options.insert(Options::ENABLE_TABLES);
    let parser = Parser::new_ext(input, options);

    let mut state = RenderState::default();
    state.wrap_width = width.max(20);

    for event in parser {
        match event {
            Event::Start(tag) => state.start(tag),
            Event::End(tag) => state.end(tag),
            Event::Text(text) => state.push_text(text.into_string()),
            Event::Code(code) => state.push_inline_code(code.into_string()),
            Event::SoftBreak => state.push_text(" ".to_string()),
            Event::HardBreak => state.finish_line(),
            // Treat markdown thematic breaks (`---`, `***`) as a single
            // paragraph boundary, not a visible rule. Claude likes to
            // sprinkle them between sections; rendering them as a
            // horizontal line read as a hard divider in the transcript.
            Event::Rule => state.block_break(),
            _ => {}
        }
    }
    state.finish_line();
    Text::from(state.lines)
}

#[derive(Default)]
struct RenderState {
    lines: Vec<Line<'static>>,
    current: Vec<Span<'static>>,
    wrap_width: u16,
    in_code_block: bool,
    list_depth: u16,
    heading: Option<HeadingLevel>,
    bold: u16,
    italic: u16,
}

impl RenderState {
    fn start(&mut self, tag: Tag<'_>) {
        match tag {
            Tag::Heading { level, .. } => {
                self.block_break();
                self.heading = Some(level);
            }
            Tag::Paragraph => self.block_break(),
            Tag::CodeBlock(_) => {
                self.block_break();
                self.in_code_block = true;
            }
            Tag::List(_) => self.list_depth = self.list_depth.saturating_add(1),
            Tag::Item => {
                self.break_line();
                let indent = "  ".repeat(self.list_depth.saturating_sub(1) as usize);
                self.current
                    .push(Span::raw(format!("{indent}• ")));
            }
            Tag::Emphasis => self.italic += 1,
            Tag::Strong => self.bold += 1,
            Tag::BlockQuote(_) => {
                self.block_break();
                self.current.push(Span::styled(
                    "▍ ",
                    Style::default().fg(Color::DarkGray),
                ));
            }
            Tag::Link { .. } => {}
            _ => {}
        }
    }

    fn end(&mut self, tag: TagEnd) {
        match tag {
            TagEnd::Heading(_) => {
                self.break_line();
                self.heading = None;
            }
            // No blank line at the end of a paragraph; the next paragraph's
            // start handles the break, and the caller decides if there
            // should be one at all.
            TagEnd::Paragraph => self.break_line(),
            TagEnd::CodeBlock => {
                self.break_line();
                self.in_code_block = false;
            }
            TagEnd::List(_) => {
                self.list_depth = self.list_depth.saturating_sub(1);
                self.break_line();
            }
            TagEnd::Item => self.break_line(),
            TagEnd::Emphasis => self.italic = self.italic.saturating_sub(1),
            TagEnd::Strong => self.bold = self.bold.saturating_sub(1),
            _ => {}
        }
    }

    fn push_text(&mut self, text: String) {
        if self.in_code_block {
            for line in text.split_inclusive('\n') {
                let trimmed = line.trim_end_matches('\n');
                self.current.push(Span::styled(
                    trimmed.to_string(),
                    Style::default().fg(Color::Cyan),
                ));
                if line.ends_with('\n') {
                    self.finish_line();
                }
            }
            return;
        }
        self.current.push(Span::styled(text, self.text_style()));
    }

    fn push_inline_code(&mut self, code: String) {
        self.current.push(Span::styled(
            code,
            Style::default()
                .fg(Color::Cyan)
                .add_modifier(Modifier::ITALIC),
        ));
    }

    /// Close the current line if there's content. Used inside blocks
    /// (list items, hard breaks). Does not emit blank spacing.
    fn break_line(&mut self) {
        if self.current.is_empty() {
            return;
        }
        let spans = std::mem::take(&mut self.current);
        self.lines.push(Line::from(spans));
    }

    /// Boundary between top-level blocks (paragraph / heading / code /
    /// blockquote). Closes the current line and inserts at most one
    /// blank between blocks, never at the very top.
    fn block_break(&mut self) {
        self.break_line();
        if self.lines.is_empty() {
            return;
        }
        if matches!(self.lines.last(), Some(line) if line.spans.is_empty()) {
            return;
        }
        self.lines.push(Line::from(""));
    }

    fn finish_line(&mut self) {
        self.break_line();
    }

    fn text_style(&self) -> Style {
        let mut style = Style::default();
        if self.bold > 0 {
            style = style.add_modifier(Modifier::BOLD);
        }
        if self.italic > 0 {
            style = style.add_modifier(Modifier::ITALIC);
        }
        if let Some(level) = self.heading {
            style = style.add_modifier(Modifier::BOLD);
            style = match level {
                HeadingLevel::H1 => style.fg(Color::Yellow),
                HeadingLevel::H2 => style.fg(Color::Magenta),
                _ => style.fg(Color::Blue),
            };
        }
        style
    }
}
