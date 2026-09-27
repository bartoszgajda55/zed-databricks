//! Parser for the diagnostics `databricks bundle validate` prints to stderr.
//!
//! The CLI (v1.x) has no machine-readable diagnostics output; `--output json` only
//! switches stdout to the resolved configuration. Diagnostics are rendered from the
//! templates in `libs/cmdio/render.go`:
//!
//! ```text
//! Warning: unknown field: bogus_field
//!   at resources.jobs.my_job
//!   in resources/job.yml:5:7
//!      databricks.yml:12:3
//!
//! Optional detail paragraph.
//! ```

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Severity {
    Error,
    Warning,
    Recommendation,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Location {
    /// Path as printed by the CLI: relative to the bundle root, or absolute.
    pub file: String,
    /// 1-based.
    pub line: u32,
    /// 1-based.
    pub column: u32,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CliDiagnostic {
    pub severity: Severity,
    pub summary: String,
    pub detail: String,
    /// Configuration paths, e.g. `resources.jobs.my_job`.
    pub paths: Vec<String>,
    pub locations: Vec<Location>,
}

enum Section {
    Summary,
    Paths,
    Locations,
    Detail,
}

fn parse_header(line: &str) -> Option<(Severity, &str)> {
    [
        ("Error: ", Severity::Error),
        ("Warning: ", Severity::Warning),
        ("Recommendation: ", Severity::Recommendation),
    ]
    .into_iter()
    .find_map(|(prefix, severity)| line.strip_prefix(prefix).map(|rest| (severity, rest)))
}

/// CLI log output (`Warn: [hostmetadata] …`) is interleaved with diagnostics on stderr.
fn is_log_line(line: &str) -> bool {
    ["Trace: ", "Debug: ", "Info: ", "Warn: "]
        .iter()
        .any(|prefix| line.starts_with(prefix))
}

/// Lines of the trailing summary block (`Name: …`, `Found 1 error`, …) end a detail paragraph.
fn is_summary_line(line: &str) -> bool {
    ["Name: ", "Target: ", "Workspace:", "Found ", "Validation OK"]
        .iter()
        .any(|prefix| line.starts_with(prefix))
}

fn parse_location(text: &str) -> Option<Location> {
    let mut parts = text.trim().rsplitn(3, ':');
    let column = parts.next()?.parse().ok()?;
    let line = parts.next()?.parse().ok()?;
    let file = parts.next()?.to_string();
    (!file.is_empty()).then_some(Location { file, line, column })
}

pub fn parse(stderr: &str) -> Vec<CliDiagnostic> {
    let mut diagnostics: Vec<CliDiagnostic> = Vec::new();
    let mut section = Section::Summary;

    for line in stderr.lines() {
        if is_log_line(line) {
            continue;
        }
        if let Some((severity, summary)) = parse_header(line) {
            diagnostics.push(CliDiagnostic {
                severity,
                summary: summary.trim().to_string(),
                detail: String::new(),
                paths: Vec::new(),
                locations: Vec::new(),
            });
            section = Section::Summary;
            continue;
        }
        let Some(current) = diagnostics.last_mut() else {
            continue;
        };

        if let Some(path) = line.strip_prefix("  at ") {
            current.paths.push(path.trim().to_string());
            section = Section::Paths;
        } else if let Some(location) = line.strip_prefix("  in ") {
            current.locations.extend(parse_location(location));
            section = Section::Locations;
        } else if let Some(continuation) = line.strip_prefix("     ") {
            match section {
                Section::Paths => current.paths.push(continuation.trim().to_string()),
                Section::Locations => current.locations.extend(parse_location(continuation)),
                Section::Summary => push_line(&mut current.summary, line.trim()),
                Section::Detail => push_line(&mut current.detail, line),
            }
        } else if line.trim().is_empty() {
            // Blank lines separate the header from the detail, and detail paragraphs.
            if matches!(section, Section::Detail) && !current.detail.is_empty() {
                current.detail.push('\n');
            }
            section = Section::Detail;
        } else if is_summary_line(line) {
            // The trailing summary block (`Name: …`, `Found …`) ends the diagnostics.
            break;
        } else if matches!(section, Section::Summary) {
            push_line(&mut current.summary, line.trim());
        } else {
            section = Section::Detail;
            push_line(&mut current.detail, line);
        }
    }

    for diagnostic in &mut diagnostics {
        diagnostic.detail = diagnostic.detail.trim().to_string();
    }
    diagnostics
}

fn push_line(target: &mut String, line: &str) {
    if !target.is_empty() && !target.ends_with('\n') {
        target.push('\n');
    }
    target.push_str(line);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_warning_with_path_and_location() {
        let diags =
            parse("Warning: unknown field: bogus_field\n  at resources.jobs.my_job\n  in resources/job.yml:5:7\n\n");
        assert_eq!(
            diags,
            vec![CliDiagnostic {
                severity: Severity::Warning,
                summary: "unknown field: bogus_field".into(),
                detail: String::new(),
                paths: vec!["resources.jobs.my_job".into()],
                locations: vec![Location {
                    file: "resources/job.yml".into(),
                    line: 5,
                    column: 7
                }],
            }]
        );
    }

    #[test]
    fn parses_multiple_locations_paths_and_detail() {
        let stderr = "\
Error: cannot merge string with int
  at variables.foo
     targets.dev.variables.foo
  in databricks.yml:10:5
     resources/a.yml:3:9

The variable is defined twice with different types.
Pick one.

Recommendation: use serverless compute
  in resources/job.yml:2:1

Name: demo
Target: dev

Found 1 error and 1 recommendation
";
        let diags = parse(stderr);
        assert_eq!(diags.len(), 2);
        assert_eq!(diags[0].severity, Severity::Error);
        assert_eq!(diags[0].paths, vec!["variables.foo", "targets.dev.variables.foo"]);
        assert_eq!(diags[0].locations.len(), 2);
        assert_eq!(
            diags[0].locations[1],
            Location {
                file: "resources/a.yml".into(),
                line: 3,
                column: 9
            }
        );
        assert_eq!(
            diags[0].detail,
            "The variable is defined twice with different types.\nPick one."
        );
        assert_eq!(diags[1].severity, Severity::Recommendation);
        assert_eq!(diags[1].detail, "");
    }

    #[test]
    fn ignores_log_lines_and_keeps_unlocated_errors() {
        let stderr = "\
Warn: [hostmetadata] failed to fetch host metadata, will skip for 1m0s
Error: Get \"https://example/api/2.0/preview/scim/v2/Me?\": no such host

Name: demo
Found 1 error
";
        let diags = parse(stderr);
        assert_eq!(diags.len(), 1);
        assert!(diags[0].summary.starts_with("Get "));
        assert!(diags[0].locations.is_empty());
    }

    #[test]
    fn log_lines_do_not_leak_into_detail() {
        let stderr = "Warning: unknown field: x\n  in a.yml:1:1\n\nWarn: [hostmetadata] failed to fetch host metadata\nError: auth failed\n";
        let diags = parse(stderr);
        assert_eq!(diags.len(), 2);
        assert_eq!(diags[0].detail, "");
        assert_eq!(diags[1].summary, "auth failed");
    }

    #[test]
    fn location_with_colon_in_path() {
        assert_eq!(
            parse_location("C:/work/databricks.yml:1:2"),
            Some(Location {
                file: "C:/work/databricks.yml".into(),
                line: 1,
                column: 2
            })
        );
        assert_eq!(parse_location("no-location"), None);
    }
}
