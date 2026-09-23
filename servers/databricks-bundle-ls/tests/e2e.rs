//! Drives the compiled server over stdio like an editor would.

use std::io::{BufRead, BufReader, Read, Write};
use std::path::Path;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

struct Client {
    child: Child,
    stdin: ChildStdin,
    messages: mpsc::Receiver<Value>,
}

impl Client {
    fn start(envs: &[(&str, &str)], init_options: Value) -> Self {
        let mut child = Command::new(env!("CARGO_BIN_EXE_databricks-bundle-ls"))
            .envs(envs.iter().copied())
            .env_remove("DATABRICKS_BUNDLE_TARGET")
            .env_remove("DATABRICKS_CONFIG_PROFILE")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let stdin = child.stdin.take().unwrap();
        let mut stdout = BufReader::new(child.stdout.take().unwrap());
        let (tx, messages) = mpsc::channel();
        std::thread::spawn(move || loop {
            let mut length = 0;
            loop {
                let mut header = String::new();
                if stdout.read_line(&mut header).unwrap_or(0) == 0 {
                    return;
                }
                let header = header.trim();
                if header.is_empty() {
                    break;
                }
                if let Some(value) = header.strip_prefix("Content-Length: ") {
                    length = value.parse().unwrap();
                }
            }
            let mut body = vec![0; length];
            stdout.read_exact(&mut body).unwrap();
            if tx.send(serde_json::from_slice(&body).unwrap()).is_err() {
                return;
            }
        });
        let mut client = Client { child, stdin, messages };
        client.send(json!({"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"capabilities": {}, "initializationOptions": init_options}}));
        client.wait_for(|m| m["id"] == 1);
        client.send(json!({"jsonrpc": "2.0", "method": "initialized", "params": {}}));
        client
    }

    fn send(&mut self, message: Value) {
        let body = message.to_string();
        write!(self.stdin, "Content-Length: {}\r\n\r\n{}", body.len(), body).unwrap();
        self.stdin.flush().unwrap();
    }

    fn wait_for(&self, predicate: impl Fn(&Value) -> bool) -> Value {
        let deadline = Instant::now() + Duration::from_secs(60);
        loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            let message = self.messages.recv_timeout(remaining).expect("timed out waiting for message");
            if predicate(&message) {
                return message;
            }
        }
    }

    fn diagnostics_for(&self, file: &Path) -> Vec<Value> {
        let uri = format!("file://{}", file.display());
        let message = self.wait_for(|m| m["method"] == "textDocument/publishDiagnostics" && m["params"]["uri"] == uri);
        message["params"]["diagnostics"].as_array().unwrap().clone()
    }

    fn open(&mut self, file: &Path) {
        self.send(json!({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {"textDocument": {
            "uri": format!("file://{}", file.display()), "languageId": "yaml", "version": 1,
            "text": std::fs::read_to_string(file).unwrap()}}}));
    }

    fn save(&mut self, file: &Path) {
        self.send(json!({"jsonrpc": "2.0", "method": "textDocument/didSave",
            "params": {"textDocument": {"uri": format!("file://{}", file.display())}}}));
    }

    fn shutdown(mut self) {
        self.send(json!({"jsonrpc": "2.0", "id": 99, "method": "shutdown"}));
        self.wait_for(|m| m["id"] == 99);
        self.send(json!({"jsonrpc": "2.0", "method": "exit"}));
        let status = self.child.wait().unwrap();
        assert!(status.success());
    }
}

fn bundle(dir: &Path) {
    std::fs::create_dir_all(dir.join("resources")).unwrap();
    std::fs::write(
        dir.join("databricks.yml"),
        "bundle:\n  name: demo\ninclude:\n  - resources/*.yml\ntargets:\n  dev:\n    default: true\n    workspace:\n      host: https://nonexistent.invalid\n",
    )
    .unwrap();
    std::fs::write(
        dir.join("resources/job.yml"),
        "resources:\n  jobs:\n    my_job:\n      name: x\n      bogus_field: 1\n",
    )
    .unwrap();
}

/// A stand-in `databricks` that records its arguments and prints canned diagnostics.
fn fake_cli(dir: &Path) -> std::path::PathBuf {
    let script = dir.join("fake-databricks");
    std::fs::write(
        &script,
        "#!/bin/sh\necho \"$@\" > \"$(dirname \"$0\")/args.txt\"\ncat >&2 <<'EOF'\nWarning: unknown field: bogus_field\n  at resources.jobs.my_job\n  in resources/job.yml:5:7\n\nName: demo\nFound 1 warning\nEOF\nexit 0\n",
    )
    .unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
    }
    script
}

#[test]
fn publishes_and_clears_diagnostics_with_the_selected_target() {
    let dir = tempfile::tempdir().unwrap();
    let project = dir.path().join("project");
    bundle(&project);
    std::fs::create_dir(project.join(".zed")).unwrap();
    std::fs::write(
        project.join(".zed/settings.json"),
        "// comment\n{ \"terminal\": { \"env\": { \"DATABRICKS_BUNDLE_TARGET\": \"dev\", \"DATABRICKS_CONFIG_PROFILE\": \"zed-dev\", } } }\n",
    )
    .unwrap();
    let cli = fake_cli(dir.path());

    let mut client = Client::start(&[], json!({"databricksPath": cli}));
    let job = project.join("resources/job.yml");
    client.open(&job);
    let diagnostics = client.diagnostics_for(&job);
    assert_eq!(diagnostics.len(), 1);
    assert_eq!(diagnostics[0]["message"], "unknown field: bogus_field");
    assert_eq!(diagnostics[0]["severity"], 2);
    assert_eq!(diagnostics[0]["source"], "databricks bundle validate (target: dev)");
    assert_eq!(diagnostics[0]["range"]["start"], json!({"line": 4, "character": 6}));
    assert_eq!(diagnostics[0]["range"]["end"], json!({"line": 4, "character": 17}));
    assert_eq!(
        std::fs::read_to_string(dir.path().join("args.txt")).unwrap().trim(),
        "bundle validate --target dev --profile zed-dev"
    );

    // Fix the file; the fake CLI now reports nothing, so the save must clear the diagnostic.
    std::fs::write(
        &cli,
        "#!/bin/sh\necho 'Name: demo' >&2\necho 'Validation OK!' >&2\n",
    )
    .unwrap();
    client.save(&job);
    assert!(client.diagnostics_for(&job).is_empty());
    client.shutdown();
}

#[test]
fn reports_missing_cli_on_the_root_config() {
    let dir = tempfile::tempdir().unwrap();
    bundle(dir.path());
    let mut client = Client::start(&[], json!({"databricksPath": dir.path().join("does-not-exist")}));
    client.open(&dir.path().join("databricks.yml"));
    let diagnostics = client.diagnostics_for(&dir.path().join("databricks.yml"));
    assert!(diagnostics[0]["message"].as_str().unwrap().contains("failed to start"));
    client.shutdown();
}

#[test]
fn real_cli_reports_schema_warnings_with_locations() {
    if Command::new("databricks").arg("--version").output().is_err() {
        eprintln!("skipping: databricks CLI not installed");
        return;
    }
    let dir = tempfile::tempdir().unwrap();
    bundle(dir.path());
    // Unresolvable host: the CLI still reports config diagnostics, then fails authentication.
    let mut client = Client::start(&[("DATABRICKS_TOKEN", "dummy"), ("DATABRICKS_CONFIG_FILE", "/dev/null")], json!({}));
    let job = dir.path().join("resources/job.yml");
    client.open(&job);
    let diagnostics = client.diagnostics_for(&job);
    assert!(
        diagnostics.iter().any(|d| d["message"] == "unknown field: bogus_field"
            && d["range"]["start"] == json!({"line": 4, "character": 6})),
        "{diagnostics:#?}"
    );
    client.shutdown();
}
