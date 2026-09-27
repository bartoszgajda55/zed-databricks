//! `databricks-bundle-ls`: publishes `databricks bundle validate` results as LSP diagnostics.
//!
//! Validation runs when a bundle file is first opened and on every save (debounced), against
//! the target chosen by [`validate::select`]. It is a thin wrapper around the CLI: no bundle
//! semantics are reimplemented here.

mod cli_output;
mod validate;

use std::collections::{BTreeSet, HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use lsp_server::{Connection, Message, Notification, Response};
use lsp_types::notification::{
    DidChangeConfiguration, DidOpenTextDocument, DidSaveTextDocument, LogMessage, Notification as _, PublishDiagnostics,
};
use lsp_types::{
    Diagnostic, DiagnosticRelatedInformation, DiagnosticSeverity, InitializeParams, Location, LogMessageParams,
    MessageType, Position, PublishDiagnosticsParams, Range, ServerCapabilities, TextDocumentSyncCapability,
    TextDocumentSyncKind, TextDocumentSyncOptions, TextDocumentSyncSaveOptions, Url,
};

use cli_output::{CliDiagnostic, Severity};
use validate::Settings;

const DEBOUNCE: Duration = Duration::from_millis(400);

type Error = Box<dyn std::error::Error + Send + Sync>;

fn main() -> Result<(), Error> {
    if std::env::args().any(|arg| arg == "--version") {
        println!("databricks-bundle-ls {}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }

    let (connection, io_threads) = Connection::stdio();
    let capabilities = ServerCapabilities {
        text_document_sync: Some(TextDocumentSyncCapability::Options(TextDocumentSyncOptions {
            open_close: Some(true),
            change: Some(TextDocumentSyncKind::NONE),
            save: Some(TextDocumentSyncSaveOptions::Supported(true)),
            ..Default::default()
        })),
        ..Default::default()
    };
    let params: InitializeParams = serde_json::from_value(connection.initialize(serde_json::to_value(capabilities)?)?)?;
    let base_options = params.initialization_options.unwrap_or_default();
    let initial = Settings::from_json(&base_options).unwrap_or_else(|err| {
        log_message(&connection.sender, MessageType::WARNING, err);
        Settings::default()
    });
    let settings = Arc::new(Mutex::new(initial));

    let (queue, jobs) = mpsc::channel::<PathBuf>();
    let worker = {
        let sender = connection.sender.clone();
        let settings = Arc::clone(&settings);
        std::thread::spawn(move || {
            Worker {
                sender,
                settings,
                published: HashMap::new(),
            }
            .run(jobs)
        })
    };

    let mut known_roots = HashSet::new();
    for message in &connection.receiver {
        match message {
            Message::Request(request) => {
                if connection.handle_shutdown(&request)? {
                    break;
                }
                connection.sender.send(Message::Response(Response::new_err(
                    request.id,
                    lsp_server::ErrorCode::MethodNotFound as i32,
                    format!("unsupported request: {}", request.method),
                )))?;
            }
            Message::Notification(notification) => {
                let context = Context {
                    base_options: &base_options,
                    settings: &settings,
                    queue: &queue,
                    sender: &connection.sender,
                };
                handle_notification(notification, &context, &mut known_roots);
            }
            Message::Response(_) => {}
        }
    }

    drop(queue);
    let _ = worker.join();
    // The stdout writer thread only exits once every sender is gone.
    drop(connection);
    io_threads.join()?;
    Ok(())
}

struct Context<'a> {
    /// Initialization options; configuration changes are merged over them.
    base_options: &'a serde_json::Value,
    settings: &'a Mutex<Settings>,
    queue: &'a mpsc::Sender<PathBuf>,
    sender: &'a crossbeam_channel::Sender<Message>,
}

fn handle_notification(notification: Notification, context: &Context, known_roots: &mut HashSet<PathBuf>) {
    let (uri, is_save) = match notification.method.as_str() {
        DidOpenTextDocument::METHOD => {
            let Ok(params) = serde_json::from_value::<lsp_types::DidOpenTextDocumentParams>(notification.params) else {
                return;
            };
            (params.text_document.uri, false)
        }
        DidSaveTextDocument::METHOD => {
            let Ok(params) = serde_json::from_value::<lsp_types::DidSaveTextDocumentParams>(notification.params) else {
                return;
            };
            (params.text_document.uri, true)
        }
        DidChangeConfiguration::METHOD => {
            let Ok(params) = serde_json::from_value::<lsp_types::DidChangeConfigurationParams>(notification.params)
            else {
                return;
            };
            match Settings::from_json(&validate::merge(context.base_options, &params.settings)) {
                Ok(new) => {
                    let mut current = context.settings.lock().unwrap();
                    if new != *current {
                        let revalidate = new.validates_on_open();
                        *current = new;
                        for root in known_roots.iter().filter(|_| revalidate) {
                            let _ = context.queue.send(root.clone());
                        }
                    }
                }
                Err(err) => log_message(context.sender, MessageType::WARNING, err),
            }
            return;
        }
        _ => return,
    };

    let Ok(path) = uri.to_file_path() else { return };
    let Some(root) = validate::find_bundle_root(&path) else {
        return;
    };
    // Opening a file validates its bundle once (unless `validateOnOpen` is off); saves always do.
    let first_open = known_roots.insert(root.clone());
    if is_save || (first_open && context.settings.lock().unwrap().validates_on_open()) {
        let _ = context.queue.send(root);
    }
}

struct Worker {
    sender: crossbeam_channel::Sender<Message>,
    settings: Arc<Mutex<Settings>>,
    /// Files that currently carry diagnostics, per bundle root, so stale ones can be cleared.
    published: HashMap<PathBuf, HashSet<Url>>,
}

impl Worker {
    fn run(mut self, jobs: mpsc::Receiver<PathBuf>) {
        while let Ok(first) = jobs.recv() {
            // Coalesce bursts (e.g. "save all") into one validation per bundle root.
            let mut roots = BTreeSet::from([first]);
            while let Ok(root) = jobs.recv_timeout(DEBOUNCE) {
                roots.insert(root);
            }
            for root in roots {
                self.validate(&root);
            }
        }
    }

    fn validate(&mut self, root: &Path) {
        let settings = self.settings.lock().unwrap().clone();
        let by_file = match validate::run(&settings, root) {
            Ok(outcome) => {
                let source = match &outcome.selection.target {
                    Some(target) => format!("databricks bundle validate (target: {target})"),
                    None => "databricks bundle validate".to_string(),
                };
                self.log(format!(
                    "{}: {} diagnostic(s) [{}]",
                    root.display(),
                    outcome.diagnostics.len(),
                    source
                ));
                to_lsp(root, &outcome.diagnostics, &source)
            }
            Err(message) => {
                self.log(message.clone());
                let file = validate::root_config_file(root);
                let diagnostic = Diagnostic {
                    range: Range::default(),
                    severity: Some(DiagnosticSeverity::ERROR),
                    source: Some("databricks-bundle-ls".into()),
                    message,
                    ..Default::default()
                };
                Url::from_file_path(file)
                    .map(|uri| HashMap::from([(uri, vec![diagnostic])]))
                    .unwrap_or_default()
            }
        };

        let previous = self.published.remove(root).unwrap_or_default();
        for stale in previous.iter().filter(|uri| !by_file.contains_key(uri)) {
            self.publish(stale.clone(), Vec::new());
        }
        self.published
            .insert(root.to_path_buf(), by_file.keys().cloned().collect());
        for (uri, diagnostics) in by_file {
            self.publish(uri, diagnostics);
        }
    }

    fn publish(&self, uri: Url, diagnostics: Vec<Diagnostic>) {
        let params = PublishDiagnosticsParams {
            uri,
            diagnostics,
            version: None,
        };
        let _ = self.sender.send(Message::Notification(Notification::new(
            PublishDiagnostics::METHOD.into(),
            params,
        )));
    }

    fn log(&self, message: String) {
        log_message(&self.sender, MessageType::INFO, message);
    }
}

fn log_message(sender: &crossbeam_channel::Sender<Message>, typ: MessageType, message: String) {
    let params = LogMessageParams { typ, message };
    let _ = sender.send(Message::Notification(Notification::new(
        LogMessage::METHOD.into(),
        params,
    )));
}

/// Groups CLI diagnostics by file. Diagnostics without a location go on the root config file.
fn to_lsp(root: &Path, diagnostics: &[CliDiagnostic], source: &str) -> HashMap<Url, Vec<Diagnostic>> {
    let mut lines_cache: HashMap<PathBuf, Vec<String>> = HashMap::new();
    let mut by_file: HashMap<Url, Vec<Diagnostic>> = HashMap::new();

    for cli in diagnostics {
        let locations: Vec<(PathBuf, Range)> = cli
            .locations
            .iter()
            .map(|location| {
                let path = root.join(&location.file);
                let lines = lines_cache.entry(path.clone()).or_insert_with(|| read_lines(&path));
                (path, token_range(lines, location.line, location.column))
            })
            .collect();

        let mut message = cli.summary.clone();
        if locations.is_empty() && !cli.paths.is_empty() {
            message.push_str(&format!("\nat {}", cli.paths.join(", ")));
        }
        if !cli.detail.is_empty() {
            message.push_str("\n\n");
            message.push_str(&cli.detail);
        }
        let severity = match cli.severity {
            Severity::Error => DiagnosticSeverity::ERROR,
            Severity::Warning => DiagnosticSeverity::WARNING,
            Severity::Recommendation => DiagnosticSeverity::INFORMATION,
        };

        let (primary_path, primary_range) = locations
            .first()
            .cloned()
            .or_else(|| locate_mentioned_path(root, &cli.summary))
            .unwrap_or_else(|| (validate::root_config_file(root), Range::default()));
        let Ok(uri) = Url::from_file_path(&primary_path) else {
            continue;
        };
        // Secondary locations (e.g. a value defined in several files) become related information.
        let related: Vec<DiagnosticRelatedInformation> = locations
            .iter()
            .skip(1)
            .filter_map(|(path, range)| {
                Some(DiagnosticRelatedInformation {
                    location: Location {
                        uri: Url::from_file_path(path).ok()?,
                        range: *range,
                    },
                    message: "also defined here".into(),
                })
            })
            .collect();

        by_file.entry(uri).or_default().push(Diagnostic {
            range: primary_range,
            severity: Some(severity),
            source: Some(source.to_string()),
            message,
            related_information: (!related.is_empty()).then_some(related),
            ..Default::default()
        });
    }
    by_file
}

/// Some CLI errors carry no location but name a file (e.g. `notebook src/x.ipynb not found`).
/// Point at the bundle YAML line that references it, if exactly one line does.
fn locate_mentioned_path(root: &Path, summary: &str) -> Option<(PathBuf, Range)> {
    let names: Vec<&str> = summary
        .split_whitespace()
        .map(|word| word.trim_matches(|c: char| matches!(c, '"' | '\'' | '`' | ',' | ':' | '(' | ')')))
        .filter(|word| word.contains('/') && word.rsplit('/').next().is_some_and(|name| name.contains('.')))
        .filter_map(|word| word.rsplit('/').next())
        .collect();
    if names.is_empty() {
        return None;
    }
    let mut matches = Vec::new();
    for file in bundle_yaml_files(root) {
        for (index, line) in read_lines(&file).iter().enumerate() {
            if line.trim_start().starts_with('#') {
                continue;
            }
            for name in &names {
                if let Some(byte) = line.find(name) {
                    let start = line[..byte].encode_utf16().count() as u32;
                    let end = start + name.encode_utf16().count() as u32;
                    matches.push((
                        file.clone(),
                        Range::new(Position::new(index as u32, start), Position::new(index as u32, end)),
                    ));
                }
            }
        }
    }
    (matches.len() == 1).then(|| matches.remove(0))
}

/// YAML files under the bundle root, skipping build output and environments.
fn bundle_yaml_files(root: &Path) -> Vec<PathBuf> {
    const SKIP: [&str; 5] = [".databricks", ".git", ".venv", "node_modules", "dist"];
    let mut files = Vec::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let Ok(entries) = std::fs::read_dir(&dir) else { continue };
        for entry in entries.flatten() {
            let path = entry.path();
            let name = entry.file_name();
            // `file_type` doesn't follow symlinks, so a symlinked directory (possibly a loop)
            // is never descended into.
            let Ok(kind) = entry.file_type() else { continue };
            if kind.is_dir() {
                if !SKIP.iter().any(|skip| name == *skip) {
                    stack.push(path);
                }
            } else if path.extension().is_some_and(|ext| ext == "yml" || ext == "yaml") {
                files.push(path);
            }
        }
    }
    files.sort();
    files
}

fn read_lines(path: &Path) -> Vec<String> {
    std::fs::read_to_string(path)
        .map(|text| text.lines().map(String::from).collect())
        .unwrap_or_default()
}

/// Range of the YAML key/token starting at the CLI's 1-based line/column (UTF-16 columns for LSP).
fn token_range(lines: &[String], line: u32, column: u32) -> Range {
    let line_index = line.saturating_sub(1);
    let start_char = column.saturating_sub(1) as usize;
    let Some(text) = lines.get(line_index as usize) else {
        let position = Position::new(line_index, 0);
        return Range::new(position, position);
    };
    let chars: Vec<char> = text.chars().collect();
    let start_char = start_char.min(chars.len());
    let token_len = chars[start_char..]
        .iter()
        .take_while(|c| !c.is_whitespace() && **c != ':')
        .count();
    let end_char = if token_len == 0 {
        chars.len()
    } else {
        start_char + token_len
    };
    let utf16 = |n: usize| chars[..n].iter().map(|c| c.len_utf16() as u32).sum::<u32>();
    Range::new(
        Position::new(line_index, utf16(start_char)),
        Position::new(line_index, utf16(end_char)),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use cli_output::Location as CliLocation;

    #[test]
    fn unlocated_error_is_placed_on_the_line_naming_the_file() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(dir.path().join("resources")).unwrap();
        std::fs::create_dir_all(dir.path().join(".databricks")).unwrap();
        std::fs::write(dir.path().join("databricks.yml"), "bundle:\n  name: x\n").unwrap();
        std::fs::write(dir.path().join(".databricks/cache.yml"), "missing.ipynb\n").unwrap();
        std::fs::write(
            dir.path().join("resources/job.yml"),
            "tasks:\n  - notebook_task:\n      # missing.ipynb (comment)\n      notebook_path: ../src/missing.ipynb\n",
        )
        .unwrap();
        let (file, range) = locate_mentioned_path(dir.path(), "notebook src/missing.ipynb not found").unwrap();
        assert_eq!(file, dir.path().join("resources/job.yml"));
        assert_eq!(range, Range::new(Position::new(3, 28), Position::new(3, 41)));
        assert_eq!(locate_mentioned_path(dir.path(), "cannot authenticate"), None);
    }

    #[cfg(unix)]
    #[test]
    fn symlinked_directories_are_not_followed() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir(dir.path().join("resources")).unwrap();
        std::fs::write(dir.path().join("resources/job.yml"), "x: 1\n").unwrap();
        // A loop: resources/loop -> the bundle root.
        std::os::unix::fs::symlink(dir.path(), dir.path().join("resources/loop")).unwrap();
        assert_eq!(bundle_yaml_files(dir.path()), [dir.path().join("resources/job.yml")]);
    }

    #[test]
    fn token_range_covers_the_key() {
        let lines = vec!["resources:".into(), "      bogus_field: 1".into()];
        assert_eq!(
            token_range(&lines, 2, 7),
            Range::new(Position::new(1, 6), Position::new(1, 17))
        );
        // Past the end of the file: an empty range at the start of the line.
        assert_eq!(
            token_range(&lines, 9, 1),
            Range::new(Position::new(8, 0), Position::new(8, 0))
        );
    }

    #[test]
    fn groups_by_file_and_falls_back_to_root_config() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir(dir.path().join("resources")).unwrap();
        std::fs::write(dir.path().join("databricks.yml"), "bundle:\n  name: x\n").unwrap();
        std::fs::write(
            dir.path().join("resources/job.yml"),
            "resources:\n  jobs:\n    j:\n      bogus: 1\n",
        )
        .unwrap();
        let diagnostics = vec![
            CliDiagnostic {
                severity: Severity::Warning,
                summary: "unknown field: bogus".into(),
                detail: String::new(),
                paths: vec!["resources.jobs.j".into()],
                locations: vec![CliLocation {
                    file: "resources/job.yml".into(),
                    line: 4,
                    column: 7,
                }],
            },
            CliDiagnostic {
                severity: Severity::Error,
                summary: "cannot authenticate".into(),
                detail: "check your profile".into(),
                paths: vec![],
                locations: vec![],
            },
        ];
        let by_file = to_lsp(dir.path(), &diagnostics, "src");
        let job = &by_file[&Url::from_file_path(dir.path().join("resources/job.yml")).unwrap()];
        assert_eq!(job[0].range, Range::new(Position::new(3, 6), Position::new(3, 11)));
        assert_eq!(job[0].severity, Some(DiagnosticSeverity::WARNING));
        let root = &by_file[&Url::from_file_path(dir.path().join("databricks.yml")).unwrap()];
        assert_eq!(root[0].message, "cannot authenticate\n\ncheck your profile");
    }
}
