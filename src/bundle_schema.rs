//! Pure helpers (no host calls), unit-tested natively.

use zed_extension_api::{serde_json, Architecture, Os};

/// Schema published with every CLI release; also what SchemaStore points `databricks.yml` at.
pub const LATEST_SCHEMA_URL: &str = "https://github.com/databricks/cli/releases/latest/download/jsonschema.json";

/// Files that are (fragments of) bundle configuration.
pub const BUNDLE_FILE_GLOBS: [&str; 6] = [
    "databricks.yml",
    "databricks.yaml",
    "*.bundle.yml",
    "*.bundle.yaml",
    "resources/**/*.yml",
    "resources/**/*.yaml",
];

/// `Databricks CLI v1.7.0` -> `1.7.0`.
pub fn parse_cli_version(output: &str) -> Option<String> {
    let token = output.split_whitespace().find(|word| word.trim_start_matches('v').starts_with(|c: char| c.is_ascii_digit()))?;
    let version = token.trim_start_matches('v');
    version
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | '+'))
        .then(|| version.to_string())
}

pub fn yaml_schema_configuration(schema: &str) -> serde_json::Value {
    serde_json::json!({ "yaml": { "schemas": { schema: BUNDLE_FILE_GLOBS } } })
}

/// Whether the user's `lsp.yaml-language-server.settings` already maps a Databricks bundle schema.
pub fn user_maps_bundle_schema(settings: Option<&serde_json::Value>) -> bool {
    let Some(schemas) = settings.and_then(|s| s.get("yaml")).and_then(|y| y.get("schemas")).and_then(|s| s.as_object()) else {
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
        options.entry("databricksPath").or_insert(serde_json::Value::String(path));
    }
    serde_json::Value::Object(options)
}

/// Rust target triple used in release asset names.
pub fn release_target(os: Os, arch: Architecture) -> Result<&'static str, String> {
    Ok(match (os, arch) {
        (Os::Mac, Architecture::Aarch64) => "aarch64-apple-darwin",
        (Os::Mac, Architecture::X8664) => "x86_64-apple-darwin",
        (Os::Linux, Architecture::Aarch64) => "aarch64-unknown-linux-gnu",
        (Os::Linux, Architecture::X8664) => "x86_64-unknown-linux-gnu",
        (Os::Windows, Architecture::X8664) => "x86_64-pc-windows-msvc",
        _ => return Err("no prebuilt databricks-bundle-ls for this platform".into()),
    })
}

/// Paths in the extension's work directory, made absolute for the host / other language servers.
pub fn absolute(relative: &str) -> String {
    std::env::current_dir()
        .map(|dir| dir.join(relative).to_string_lossy().into_owned())
        .unwrap_or_else(|_| relative.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn parses_cli_version() {
        assert_eq!(parse_cli_version("Databricks CLI v1.7.0\n").as_deref(), Some("1.7.0"));
        assert_eq!(parse_cli_version("Databricks CLI v0.250.0-dev+abc").as_deref(), Some("0.250.0-dev+abc"));
        assert_eq!(parse_cli_version("garbage"), None);
        assert_eq!(parse_cli_version("Databricks CLI v1.7.0/../../x"), None);
    }

    #[test]
    fn builds_yaml_configuration() {
        let config = yaml_schema_configuration("/w/bundle-schema-1.7.0.json");
        assert_eq!(config["yaml"]["schemas"]["/w/bundle-schema-1.7.0.json"][0], "databricks.yml");
    }

    #[test]
    fn detects_user_bundle_mapping() {
        assert!(!user_maps_bundle_schema(None));
        assert!(!user_maps_bundle_schema(Some(&json!({"yaml": {"schemas": {"https://json.schemastore.org/github-workflow.json": ".github/workflows/*"}}}))));
        assert!(user_maps_bundle_schema(Some(&json!({"yaml": {"schemas": {"./.zed/databricks-bundle.schema.json": ["x.yml"]}}}))));
        assert!(user_maps_bundle_schema(Some(&json!({"yaml": {"schemas": {"https://example/schema.json": "databricks.yml"}}}))));
    }

    #[test]
    fn initialization_options_keep_user_values() {
        let merged = initialization_options(Some(json!({"target": "dev", "databricksPath": "/custom"})), Some("/usr/bin/databricks".into()));
        assert_eq!(merged, json!({"target": "dev", "databricksPath": "/custom"}));
        assert_eq!(initialization_options(None, Some("/d".into())), json!({"databricksPath": "/d"}));
        assert_eq!(initialization_options(Some(json!(null)), None), json!({}));
    }
}
