//! Renderer for the TUI. One `draw(frame, app, editor)` entry point;
//! layout decisions live here, content rendering in the submodules.

pub mod editor;
pub mod footer;
pub mod sidebar;
pub mod transcript;

use ratatui::Frame;
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::widgets::{Block, Borders};
use tui_textarea::TextArea;

use crate::tui::state::{AppState, Pane};

pub fn draw(frame: &mut Frame<'_>, app: &mut AppState, editor: &TextArea<'_>) {
    let size = frame.area();
    let outer = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Min(5),
            Constraint::Length(editor_height(editor, size.width)),
            Constraint::Length(1),
        ])
        .split(size);

    let main_area = outer[0];
    let editor_area = outer[1];
    let footer_area = outer[2];

    if app.sidebar_visible {
        let split = Layout::default()
            .direction(Direction::Horizontal)
            .constraints([Constraint::Min(40), Constraint::Length(38)])
            .split(main_area);
        transcript::draw(frame, split[0], app);
        sidebar::draw(frame, split[1], app);
    } else {
        transcript::draw(frame, main_area, app);
    }

    editor::draw(frame, editor_area, app, editor);
    footer::draw(frame, footer_area, app);
}

fn editor_height(editor: &TextArea<'_>, width: u16) -> u16 {
    let body_width = width.saturating_sub(4).max(20) as usize;
    let mut lines: u16 = 0;
    for line in editor.lines() {
        let len = line.chars().count();
        let visible = if line.is_empty() {
            1
        } else {
            (len + body_width - 1) / body_width
        };
        lines = lines.saturating_add(visible.max(1) as u16);
    }
    lines.clamp(3, 10) + 2
}

pub fn focus_border(app: &AppState, pane: Pane) -> Block<'_> {
    let style = if app.focused_pane == pane {
        Style::default()
            .fg(Color::Cyan)
            .add_modifier(Modifier::BOLD)
    } else {
        Style::default().fg(Color::DarkGray)
    };
    Block::default().borders(Borders::ALL).border_style(style)
}

pub fn inner_area(area: Rect) -> Rect {
    Rect {
        x: area.x.saturating_add(1),
        y: area.y.saturating_add(1),
        width: area.width.saturating_sub(2),
        height: area.height.saturating_sub(2),
    }
}
