//! corral 原生加速层。
//!
//! 只做「大量输入压缩成少量结果」的活：把一屏带 ANSI 转义的文本直接解析成
//! 若干紧凑的行元组，全程不构建深层 Python 对象树（实测约 27 倍于纯 Python）。
//!
//! 这里曾经还有一个 serde_json + PyO3 的 `loads`，实测比标准库 C 实现的 json
//! **慢约 2.5 倍**，已于 v0.24.22 移除：产出物是一大棵 Python dict 时，
//! Rust 侧要先解析成中间对象树再逐节点转换，等于把同一份数据构建两遍。
//! 不要再加回来，详见 docs/PERFORMANCE_KNOWLEDGE_BASE.md。

use pyo3::prelude::*;
use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};
use unicode_width::UnicodeWidthChar;

#[derive(Clone, Copy, Debug, Default, Eq, Hash, PartialEq)]
enum Colour {
    #[default]
    Default,
    Indexed(u8),
    Rgb(u8, u8, u8),
}

#[derive(Clone, Copy, Debug, Default, Eq, Hash, PartialEq)]
struct StyleState {
    fg: Colour,
    bg: Colour,
    bold: bool,
    dim: bool,
    underline: bool,
    reverse: bool,
}

impl StyleState {
    fn apply(&mut self, params: &[i32]) {
        let mut i = 0;
        while i < params.len() {
            let p = params[i];
            match p {
                0 => *self = Self::default(),
                1 => self.bold = true,
                2 => self.dim = true,
                4 => self.underline = true,
                7 => self.reverse = true,
                22 => {
                    self.bold = false;
                    self.dim = false;
                }
                24 => self.underline = false,
                27 => self.reverse = false,
                39 => self.fg = Colour::Default,
                49 => self.bg = Colour::Default,
                30..=37 => self.fg = Colour::Indexed((p - 30) as u8),
                40..=47 => self.bg = Colour::Indexed((p - 40) as u8),
                90..=97 => self.fg = Colour::Indexed((p - 90 + 8) as u8),
                100..=107 => self.bg = Colour::Indexed((p - 100 + 8) as u8),
                38 | 48 => {
                    let mut colour = None;
                    if i + 2 < params.len() && params[i + 1] == 5 {
                        colour = Some(Colour::Indexed(params[i + 2].clamp(0, 255) as u8));
                        i += 2;
                    } else if i + 4 < params.len() && params[i + 1] == 2 {
                        colour = Some(Colour::Rgb(
                            params[i + 2].clamp(0, 255) as u8,
                            params[i + 3].clamp(0, 255) as u8,
                            params[i + 4].clamp(0, 255) as u8,
                        ));
                        i += 4;
                    } else {
                        i += 1;
                    }
                    if let Some(colour) = colour {
                        if p == 38 {
                            self.fg = colour;
                        } else {
                            self.bg = colour;
                        }
                    }
                }
                _ => {}
            }
            i += 1;
        }
    }
}

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
struct Cell {
    text: String,
    style: StyleState,
    continuation: bool,
}

impl Default for Cell {
    fn default() -> Self {
        Self {
            text: " ".to_string(),
            style: StyleState::default(),
            continuation: false,
        }
    }
}

type ColourTuple = (i8, u8, u8, u8);
type SpanTuple = (
    usize,
    usize,
    ColourTuple,
    ColourTuple,
    bool,
    bool,
    bool,
    bool,
);
type RowTuple = (String, Vec<SpanTuple>, u64);

fn colour_tuple(colour: Colour) -> ColourTuple {
    match colour {
        Colour::Default => (-1, 0, 0, 0),
        Colour::Indexed(value) => (0, value, 0, 0),
        Colour::Rgb(r, g, b) => (1, r, g, b),
    }
}

fn parse_params(body: &str) -> Vec<i32> {
    if body.is_empty() {
        return vec![0];
    }
    let mut values = Vec::new();
    for part in body.split(';') {
        match if part.is_empty() {
            Ok(0)
        } else {
            part.parse::<i32>()
        } {
            Ok(value) => values.push(value),
            Err(_) => return Vec::new(),
        }
    }
    values
}

fn parse_line(line: &str, width: usize) -> Vec<Cell> {
    let mut row = vec![Cell::default(); width];
    let chars: Vec<char> = line.chars().collect();
    let mut state = StyleState::default();
    let mut x = 0usize;
    let mut i = 0usize;
    while i < chars.len() && x < width {
        if chars[i] == '\u{1b}' || chars[i] == '\u{9b}' {
            if chars[i] == '\u{9b}' || (i + 1 < chars.len() && chars[i + 1] == '[') {
                let body_start = if chars[i] == '\u{9b}' { i + 1 } else { i + 2 };
                let mut j = body_start;
                while j < chars.len() && !(('@'..='~').contains(&chars[j])) {
                    j += 1;
                }
                if j >= chars.len() {
                    break;
                }
                if chars[j] == 'm' {
                    let body: String = chars[body_start..j].iter().collect();
                    state.apply(&parse_params(&body));
                }
                i = j + 1;
                continue;
            }
            if chars[i] == '\u{9b}' {
                break;
            }
            // 字符串型序列（OSC / DCS / SOS / PM / APC）：载荷整段丢弃，只留可见
            // 文字；细节见 Python 参考实现 `_parse_line` 的同一分支注释。
            if i + 1 < chars.len() && matches!(chars[i + 1], ']' | 'P' | 'X' | '^' | '_') {
                let mut j = i + 2;
                let mut terminated = false;
                while j < chars.len() {
                    if matches!(chars[j], '\u{7}' | '\u{9c}') {
                        j += 1;
                        terminated = true;
                        break;
                    }
                    if chars[j] == '\u{1b}' && j + 1 < chars.len() && chars[j + 1] == '\\' {
                        j += 2;
                        terminated = true;
                        break;
                    }
                    j += 1;
                }
                if !terminated {
                    break;
                }
                i = j;
                continue;
            }
            let mut j = i + 1;
            while j < chars.len() && (' '..='/').contains(&chars[j]) {
                j += 1;
            }
            if j < chars.len() && ('0'..='~').contains(&chars[j]) {
                j += 1;
            }
            i = j;
            continue;
        }
        // ECMA-48 的 8-bit C1 形式：OSC/DCS/SOS/PM/APC 也必须整段跳到
        // BEL、ST（0x9C）或两字节 ST（ESC \），否则 OSC 8 的 `8;;` 会漏进正文。
        if matches!(
            chars[i],
            '\u{90}' | '\u{98}' | '\u{9d}' | '\u{9e}' | '\u{9f}'
        ) {
            let mut j = i + 1;
            let mut terminated = false;
            while j < chars.len() {
                if matches!(chars[j], '\u{7}' | '\u{9c}') {
                    j += 1;
                    terminated = true;
                    break;
                }
                if chars[j] == '\u{1b}' && j + 1 < chars.len() && chars[j + 1] == '\\' {
                    j += 2;
                    terminated = true;
                    break;
                }
                j += 1;
            }
            if !terminated {
                break;
            }
            i = j;
            continue;
        }
        let ch = chars[i];
        let char_width = UnicodeWidthChar::width(ch).unwrap_or(0);
        if char_width == 0 {
            if x > 0 && !row[x - 1].continuation {
                row[x - 1].text.push(ch);
            }
            i += 1;
            continue;
        }
        row[x] = Cell {
            text: ch.to_string(),
            style: state,
            continuation: false,
        };
        if char_width >= 2 {
            if x + 1 >= width {
                row[x] = Cell::default();
                x += 1;
            } else {
                row[x + 1] = Cell {
                    text: " ".to_string(),
                    style: state,
                    continuation: true,
                };
                x += 2;
            }
        } else {
            x += 1;
        }
        i += 1;
    }
    row
}

fn compile_row(row: &[Cell]) -> RowTuple {
    let mut text = String::new();
    let mut spans = Vec::new();
    let mut char_pos = 0usize;
    let mut span_start = 0usize;
    let mut current: Option<StyleState> = None;
    for cell in row {
        if cell.continuation {
            continue;
        }
        text.push_str(&cell.text);
        if current != Some(cell.style) {
            if let Some(style) = current {
                if char_pos > span_start {
                    spans.push((
                        span_start,
                        char_pos,
                        colour_tuple(style.fg),
                        colour_tuple(style.bg),
                        style.bold,
                        style.dim,
                        style.underline,
                        style.reverse,
                    ));
                }
            }
            span_start = char_pos;
            current = Some(cell.style);
        }
        char_pos += cell.text.chars().count();
    }
    if let Some(style) = current {
        if char_pos > span_start {
            spans.push((
                span_start,
                char_pos,
                colour_tuple(style.fg),
                colour_tuple(style.bg),
                style.bold,
                style.dim,
                style.underline,
                style.reverse,
            ));
        }
    }
    let mut hasher = DefaultHasher::new();
    row.hash(&mut hasher);
    (text, spans, hasher.finish())
}

#[pyfunction]
fn parse_ansi_rows(py: Python<'_>, text: &str, width: usize, height: usize) -> Vec<RowTuple> {
    py.allow_threads(|| {
        let mut rows: Vec<RowTuple> = text
            .split('\n')
            .take(height)
            .map(|line| compile_row(&parse_line(line, width)))
            .collect();
        let blank = compile_row(&vec![Cell::default(); width]);
        while rows.len() < height {
            rows.push(blank.clone());
        }
        rows
    })
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(parse_ansi_rows, module)?)?;
    module.add("ACCELERATOR_VERSION", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
