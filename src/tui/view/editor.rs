//! Editor pane wrapping `tui_textarea::TextArea`.

use ratatui::Frame;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::Span;
use tui_textarea::TextArea;

use crate::tui::state::{AppState, Pane};
use crate::tui::view::focus_border;

pub fn draw(frame: &mut Frame<'_>, area: Rect, app: &AppState, editor: &TextArea<'_>) {
    let title = match app.focused_pane {
        Pane::Editor => " > input (Enter to send, Shift+Enter newline, Esc cancel) ",
        _ => " > input ",
    };
    let block = focus_border(app, Pane::Editor).title(Span::styled(
        title,
        Style::default()
            .fg(Color::Cyan)
            .add_modifier(Modifier::BOLD),
    ));
    let mut editor = editor.clone();
    editor.set_block(block);
    if app.focused_pane != Pane::Editor {
        editor.set_cursor_style(Style::default());
    } else {
        editor.set_cursor_style(Style::default().add_modifier(Modifier::REVERSED));
    }
    frame.render_widget(&editor, area);
}
