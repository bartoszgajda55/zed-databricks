//! Pure helpers (no host calls), unit-tested natively.

use zed_extension_api::serde_json;

/// Schema published with every CLI release; also what SchemaStore points `databricks.yml` at.
pub const LATEST_SCHEMA_URL: &str = "https://github.com/databricks/cli/releases/latest/download/jsonschema.json";

/// Schema files this extension generated (`bundle-schema-<cli version>.json`).
pub fn is_schema_file(name: &str) -> bool {
    name.starts_with("bundle-schema-") && name.ends_with(".json")
}

/// Files that are (fragments of) bundle configuration, relative to a bundle root.
const BUNDLE_FILE_GLOBS: [&str; 6] = [
    "databricks.yml",
    "databricks.yaml",
    "**/*.bundle.yml",
    "**/*.bundle.yaml",
    "resources/**/*.yml",
    "resources/**/*.yaml",
];

/// Bundle roots (relative to the worktree) whose files get the bundle schema: the `bundleRoots`
/// setting if present (`settings` first, then `initialization_options`), otherwise the worktree
/// root when it contains `databricks.yml`.
pub fn bundle_roots(sources: &[Option<&serde_json::Value>], root_is_bundle: bool) -> Vec<String> {
    let setting = sources.iter().flatten().find_map(|source| source.get("bundleRoots"));
    match setting.and_then(|roots| roots.as_array()) {
        Some(roots) => roots
            .iter()
            .filter_map(|root| root.as_str())
            .map(String::from)
            .collect(),
        None if root_is_bundle => vec![".".into()],
        None => Vec::new(),
    }
}

/// Absolute globs for the bundle files under `bundle_roots`.
///
/// yaml-language-server prefixes every glob with `**/` and matches it against the whole file URI,
/// so a relative `resources/**/*.yml` would also claim `src/main/resources/app.yml`. Anchoring
/// the globs at each bundle root keeps the schema off unrelated YAML.
pub fn bundle_file_globs(worktree_root: &str, bundle_roots: &[String]) -> Vec<String> {
    let worktree_root = glob_path(worktree_root);
    bundle_roots
        .iter()
        .flat_map(|root| {
            let relative = glob_path(root.trim_start_matches("./"));
            let base = match relative.trim_matches('/') {
                "" | "." => worktree_root.clone(),
                relative => format!("{worktree_root}/{relative}"),
            };
            BUNDLE_FILE_GLOBS.iter().map(move |glob| format!("{base}/{glob}"))
        })
        .collect()
}

/// A filesystem path as it appears in yaml-language-server's normalized file URIs: forward
/// slashes, lower-case drive letter, glob metacharacters escaped, no trailing slash.
fn glob_path(path: &str) -> String {
    let has_drive = path.as_bytes().get(1) == Some(&b':');
    let mut out = String::with_capacity(path.len());
    for (index, c) in path.chars().enumerate() {
        match c {
            '\\' => out.push('/'),
            '*' | '?' | '[' | ']' | '{' | '}' | '(' | ')' | '!' | '+' | '@' => {
                out.push('\\');
                out.push(c);
            }
            c if index == 0 && has_drive => out.push(c.to_ascii_lowercase()),
            c => out.push(c),
        }
    }
    out.trim_end_matches('/').to_string()
}

pub fn yaml_schema_configuration(schema: &str, globs: &[String]) -> serde_json::Value {
    serde_json::json!({ "yaml": { "schemas": { schema: globs } } })
}

/// `Databricks CLI v1.7.0` -> `1.7.0`.
pub fn parse_cli_version(output: &str) -> Option<String> {
    let token = output
        .split_whitespace()
        .find(|word| word.trim_start_matches('v').starts_with(|c: char| c.is_ascii_digit()))?;
    let version = token.trim_start_matches('v');
    version
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | '+'))
        .then(|| version.to_string())
}

/// Whether the user's `lsp.yaml-language-server.settings` already maps a Databricks bundle schema.
pub fn user_maps_bundle_schema(settings: Option<&serde_json::Value>) -> bool {
    let Some(schemas) = settings
        .and_then(|s| s.get("yaml"))
        .and_then(|y| y.get("schemas"))
        .and_then(|s| s.as_object())
    else {
        return false;
    };
    schemas.iter().any(|(schema, globs)| {
        schema.contains("databricks")
            || globs
                .as_array()
                .into_iter()
                .flatten()
                .chain(std::iter::once(globs))
                .filter_map(|glob| glob.as_str())
                .any(|glob| glob.contains("databricks.y"))
    })
}

/// User initialization options, plus the resolved CLI path unless the user set one.
pub fn initialization_options(user: Option<serde_json::Value>, databricks_path: Option<String>) -> serde_json::Value {
    let mut options = match user {
        Some(serde_json::Value::Object(map)) => map,
        _ => serde_json::Map::new(),
    };
    if let Some(path) = databricks_path {
        options
            .entry("databricksPath")
            .or_insert(serde_json::Value::String(path));
    }
    serde_json::Value::Object(options)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn recognizes_generated_schema_files() {
        assert!(is_schema_file("bundle-schema-1.7.0.json"));
        assert!(!is_schema_file("databricks-bundle-ls-0.2.1"));
    }

    #[test]
    fn parses_cli_version() {
        assert_eq!(parse_cli_version("Databricks CLI v1.7.0\n").as_deref(), Some("1.7.0"));
        assert_eq!(
            parse_cli_version("Databricks CLI v0.250.0-dev+abc").as_deref(),
            Some("0.250.0-dev+abc")
        );
        assert_eq!(parse_cli_version("garbage"), None);
        assert_eq!(parse_cli_version("Databricks CLI v1.7.0/../../x"), None);
    }

    #[test]
    fn bundle_roots_come_from_settings_or_detection() {
        assert_eq!(bundle_roots(&[None], true), ["."]);
        assert!(bundle_roots(&[None], false).is_empty());
        assert!(bundle_roots(&[Some(&json!({"strict": true}))], false).is_empty());
        let settings = json!({"bundleRoots": ["bundles/etl", "bundles/ml"]});
        assert_eq!(
            bundle_roots(&[None, Some(&settings)], true),
            ["bundles/etl", "bundles/ml"]
        );
        assert!(bundle_roots(&[Some(&json!({"bundleRoots": []})), Some(&settings)], true).is_empty());
    }

    #[test]
    fn globs_are_anchored_at_bundle_roots() {
        let globs = bundle_file_globs("/home/me/proj/", &[".".into()]);
        assert_eq!(globs[0], "/home/me/proj/databricks.yml");
        assert!(globs.contains(&"/home/me/proj/resources/**/*.yml".to_string()));
        let nested = bundle_file_globs("/home/me/proj", &["./bundles/etl/".into()]);
        assert_eq!(nested[0], "/home/me/proj/bundles/etl/databricks.yml");
        assert_eq!(
            bundle_file_globs("C:\\Users\\me\\proj", &[".".into()])[0],
            "c:/Users/me/proj/databricks.yml"
        );
        assert_eq!(
            bundle_file_globs("/w/[x] (y)", &[".".into()])[0],
            "/w/\\[x\\] \\(y\\)/databricks.yml"
        );
    }

    #[test]
    fn builds_yaml_configuration() {
        let globs = vec!["/w/databricks.yml".to_string()];
        let config = yaml_schema_configuration("/x/bundle-schema-1.7.0.json", &globs);
        assert_eq!(
            config["yaml"]["schemas"]["/x/bundle-schema-1.7.0.json"][0],
            "/w/databricks.yml"
        );
    }

    #[test]
    fn detects_user_bundle_mapping() {
        assert!(!user_maps_bundle_schema(None));
        assert!(!user_maps_bundle_schema(Some(
            &json!({"yaml": {"schemas": {"https://json.schemastore.org/github-workflow.json": ".github/workflows/*"}}})
        )));
        assert!(user_maps_bundle_schema(Some(
            &json!({"yaml": {"schemas": {"./.zed/databricks-bundle.schema.json": ["x.yml"]}}})
        )));
        assert!(user_maps_bundle_schema(Some(
            &json!({"yaml": {"schemas": {"https://example/schema.json": "databricks.yml"}}})
        )));
    }

    #[test]
    fn initialization_options_keep_user_values() {
        let merged = initialization_options(
            Some(json!({"target": "dev", "databricksPath": "/custom"})),
            Some("/usr/bin/databricks".into()),
        );
        assert_eq!(merged, json!({"target": "dev", "databricksPath": "/custom"}));
        assert_eq!(
            initialization_options(None, Some("/d".into())),
            json!({"databricksPath": "/d"})
        );
        assert_eq!(initialization_options(Some(json!(null)), None), json!({}));
    }
}
