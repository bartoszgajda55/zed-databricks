//! Running `databricks bundle validate` for a bundle root and resolving which target it runs against.

use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Duration;

use serde::Deserialize;
use wait_timeout::ChildExt;

use crate::cli_output::{self, CliDiagnostic};

pub const TARGET_ENV: &str = "DATABRICKS_BUNDLE_TARGET";
pub const PROFILE_ENV: &str = "DATABRICKS_CONFIG_PROFILE";

/// Server settings, from `initializationOptions` or `workspace/didChangeConfiguration`.
/// In Zed: `"lsp": { "databricks-bundle-ls": { "initialization_options": { … } } }`.
#[derive(Debug, Clone, Default, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase", default)]
pub struct Settings {
    pub target: Option<String>,
    pub profile: Option<String>,
    pub databricks_path: Option<String>,
    /// Pass `--strict` so warnings fail validation.
    pub strict: bool,
    pub timeout_seconds: Option<u64>,
}

impl Settings {
    /// Accepts both the bare settings object and one nested under a `databricks` key.
    pub fn from_json(value: &serde_json::Value) -> Self {
        let value = value.get("databricks").unwrap_or(value);
        serde_json::from_value(value.clone()).unwrap_or_default()
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Selection {
    pub target: Option<String>,
    pub profile: Option<String>,
}

/// Target/profile precedence: server settings > process environment > the project's
/// `.zed/settings.json` `terminal.env` (what the Zed tasks use) > CLI defaults.
pub fn select(settings: &Settings, bundle_root: &Path, env: impl Fn(&str) -> Option<String>) -> Selection {
    let terminal_env = find_zed_terminal_env(bundle_root);
    let pick = |explicit: &Option<String>, name: &str| {
        explicit
            .clone()
            .or_else(|| env(name))
            .or_else(|| terminal_env.as_ref().and_then(|e| e.get(name)?.as_str().map(String::from)))
            .filter(|value| !value.is_empty())
    };
    Selection {
        target: pick(&settings.target, TARGET_ENV),
        profile: pick(&settings.profile, PROFILE_ENV),
    }
}

fn find_zed_terminal_env(start: &Path) -> Option<serde_json::Map<String, serde_json::Value>> {
    start.ancestors().find_map(|dir| {
        let text = std::fs::read_to_string(dir.join(".zed/settings.json")).ok()?;
        let settings: serde_json::Value = serde_json::from_str(&strip_jsonc(&text)).ok()?;
        settings.get("terminal")?.get("env")?.as_object().cloned()
    })
}

/// Removes `//` and `/* */` comments and trailing commas so Zed's JSONC settings parse as JSON.
pub fn strip_jsonc(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut chars = text.chars().peekable();
    let mut in_string = false;
    while let Some(c) = chars.next() {
        if in_string {
            out.push(c);
            match c {
                '\\' => out.extend(chars.next()),
                '"' => in_string = false,
                _ => {}
            }
            continue;
        }
        match (c, chars.peek()) {
            ('"', _) => {
                in_string = true;
                out.push(c);
            }
            ('/', Some('/')) => {
                while chars.peek().is_some_and(|&c| c != '\n') {
                    chars.next();
                }
            }
            ('/', Some('*')) => {
                chars.next();
                let mut prev = ' ';
                for c in chars.by_ref() {
                    if prev == '*' && c == '/' {
                        break;
                    }
                    prev = c;
                }
            }
            _ => out.push(c),
        }
    }
    remove_trailing_commas(&out)
}

fn remove_trailing_commas(text: &str) -> String {
    let chars: Vec<char> = text.chars().collect();
    let mut out = String::with_capacity(text.len());
    let mut in_string = false;
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        if in_string {
            if c == '\\' {
                out.push(c);
                i += 1;
                if i < chars.len() {
                    out.push(chars[i]);
                }
            } else {
                if c == '"' {
                    in_string = false;
                }
                out.push(c);
            }
        } else if c == '"' {
            in_string = true;
            out.push(c);
        } else if c == ',' {
            let next = chars[i + 1..].iter().find(|c| !c.is_whitespace());
            if !matches!(next, Some('}') | Some(']')) {
                out.push(c);
            }
        } else {
            out.push(c);
        }
        i += 1;
    }
    out
}

/// The directory containing `databricks.yml`/`databricks.yaml`, searching upwards from `file`.
pub fn find_bundle_root(file: &Path) -> Option<PathBuf> {
    file.ancestors()
        .skip(1)
        .find(|dir| dir.join("databricks.yml").is_file() || dir.join("databricks.yaml").is_file())
        .map(Path::to_path_buf)
}

pub fn root_config_file(bundle_root: &Path) -> PathBuf {
    let yml = bundle_root.join("databricks.yml");
    if yml.is_file() {
        yml
    } else {
        bundle_root.join("databricks.yaml")
    }
}

pub struct Outcome {
    pub diagnostics: Vec<CliDiagnostic>,
    pub selection: Selection,
}

pub fn command_args(settings: &Settings, selection: &Selection) -> Vec<String> {
    let mut args = vec!["bundle".to_string(), "validate".to_string()];
    if let Some(target) = &selection.target {
        args.extend(["--target".to_string(), target.clone()]);
    }
    if let Some(profile) = &selection.profile {
        args.extend(["--profile".to_string(), profile.clone()]);
    }
    if settings.strict {
        args.push("--strict".to_string());
    }
    args
}

pub fn run(settings: &Settings, bundle_root: &Path) -> Result<Outcome, String> {
    let selection = select(settings, bundle_root, |name| std::env::var(name).ok());
    let program = settings.databricks_path.clone().unwrap_or_else(|| "databricks".to_string());
    let mut child = Command::new(&program)
        .args(command_args(settings, &selection))
        .current_dir(bundle_root)
        .env("NO_COLOR", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|err| format!("failed to start `{program}`: {err}"))?;

    // Read stderr on a thread so a chatty CLI can't block on a full pipe while we wait.
    let mut stderr_pipe = child.stderr.take().expect("stderr is piped");
    let reader = std::thread::spawn(move || {
        let mut stderr = String::new();
        let _ = stderr_pipe.read_to_string(&mut stderr);
        stderr
    });

    let timeout = Duration::from_secs(settings.timeout_seconds.unwrap_or(120));
    if child.wait_timeout(timeout).map_err(|err| err.to_string())?.is_none() {
        let _ = child.kill();
        let _ = child.wait();
        return Err(format!("`{program} bundle validate` timed out after {}s", timeout.as_secs()));
    }
    let stderr = reader.join().unwrap_or_default();
    Ok(Outcome { diagnostics: cli_output::parse(&stderr), selection })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strips_comments_and_trailing_commas() {
        let text = r#"// top
        {
          "a": "http://x", /* inline */
          "b": ["//not a comment", 2,],
          "c": {"d": 1,},
        }"#;
        let value: serde_json::Value = serde_json::from_str(&strip_jsonc(text)).unwrap();
        assert_eq!(value["a"], "http://x");
        assert_eq!(value["b"][0], "//not a comment");
        assert_eq!(value["c"]["d"], 1);
    }

    #[test]
    fn selection_precedence() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir(dir.path().join(".zed")).unwrap();
        std::fs::write(
            dir.path().join(".zed/settings.json"),
            r#"{ "terminal": { "env": { "DATABRICKS_BUNDLE_TARGET": "staging", "DATABRICKS_CONFIG_PROFILE": "p1" } } }"#,
        )
        .unwrap();
        let bundle = dir.path().join("bundle");
        std::fs::create_dir(&bundle).unwrap();

        let none = |_: &str| None;
        let from_zed = select(&Settings::default(), &bundle, none);
        assert_eq!(from_zed.target.as_deref(), Some("staging"));
        assert_eq!(from_zed.profile.as_deref(), Some("p1"));

        let env = |name: &str| (name == TARGET_ENV).then(|| "prod".to_string());
        assert_eq!(select(&Settings::default(), &bundle, env).target.as_deref(), Some("prod"));

        let explicit = Settings { target: Some("dev".into()), ..Default::default() };
        assert_eq!(select(&explicit, &bundle, env).target.as_deref(), Some("dev"));
    }

    #[test]
    fn settings_accept_nested_and_camel_case() {
        let s = Settings::from_json(&serde_json::json!({"databricks": {"target": "dev", "databricksPath": "/bin/db", "strict": true}}));
        assert_eq!(s.target.as_deref(), Some("dev"));
        assert_eq!(s.databricks_path.as_deref(), Some("/bin/db"));
        assert!(s.strict);
        assert_eq!(Settings::from_json(&serde_json::json!(null)), Settings::default());
    }

    #[test]
    fn finds_bundle_root_from_resource_file() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(dir.path().join("resources")).unwrap();
        std::fs::write(dir.path().join("databricks.yml"), "bundle: {name: x}\n").unwrap();
        let file = dir.path().join("resources/job.yml");
        assert_eq!(find_bundle_root(&file).as_deref(), Some(dir.path()));
        assert_eq!(find_bundle_root(&dir.path().join("databricks.yml")).as_deref(), Some(dir.path()));
    }
}
