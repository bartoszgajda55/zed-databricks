//! Where `databricks-bundle-ls` comes from: release asset names, binary names and the copies
//! cached in the extension's work directory. Pure helpers (no host calls), unit-tested natively.

use zed_extension_api::{Architecture, Os};

pub const SERVER_NAME: &str = "databricks-bundle-ls";

/// Rust target triple used in release asset names.
pub fn release_target(os: Os, arch: Architecture) -> Result<&'static str, String> {
    Ok(match (os, arch) {
        (Os::Mac, Architecture::Aarch64) => "aarch64-apple-darwin",
        (Os::Mac, Architecture::X8664) => "x86_64-apple-darwin",
        (Os::Linux, Architecture::Aarch64) => "aarch64-unknown-linux-gnu",
        (Os::Linux, Architecture::X8664) => "x86_64-unknown-linux-gnu",
        (Os::Windows, Architecture::X8664) => "x86_64-pc-windows-msvc",
        _ => return Err(format!("no prebuilt {SERVER_NAME} for this platform")),
    })
}

pub fn server_binary_name(os: Os) -> String {
    match os {
        Os::Windows => format!("{SERVER_NAME}.exe"),
        _ => SERVER_NAME.to_string(),
    }
}

/// Directory a downloaded server version is unpacked into, e.g. `databricks-bundle-ls-0.2.1`.
pub fn version_dir(version: &str) -> String {
    format!("{SERVER_NAME}-{version}")
}

/// The newest of the cached server directories (`databricks-bundle-ls-<version>`), used when
/// the pinned version can't be downloaded (offline, or GitHub's API rate limit).
pub fn newest_cached_version(dir_names: impl IntoIterator<Item = String>) -> Option<String> {
    let prefix = format!("{SERVER_NAME}-");
    dir_names
        .into_iter()
        .filter_map(|name| {
            let version = name.strip_prefix(&prefix)?;
            let key: Vec<u64> = version
                .split(['.', '-'])
                .map(|part| part.parse().unwrap_or(0))
                .collect();
            Some((key, name))
        })
        .max()
        .map(|(_, name)| name)
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

    #[test]
    fn names_follow_release_assets() {
        assert_eq!(
            release_target(Os::Windows, Architecture::X8664),
            Ok("x86_64-pc-windows-msvc")
        );
        assert!(release_target(Os::Windows, Architecture::Aarch64).is_err());
        assert_eq!(server_binary_name(Os::Windows), "databricks-bundle-ls.exe");
        assert_eq!(server_binary_name(Os::Linux), "databricks-bundle-ls");
        assert_eq!(version_dir("0.2.1"), "databricks-bundle-ls-0.2.1");
    }

    #[test]
    fn picks_the_newest_cached_version_numerically() {
        let names = [
            "databricks-bundle-ls-0.9.0",
            "bundle-schema-1.7.0.json",
            "databricks-bundle-ls-0.10.0",
        ];
        assert_eq!(
            newest_cached_version(names.map(String::from)).as_deref(),
            Some("databricks-bundle-ls-0.10.0")
        );
        assert_eq!(newest_cached_version(["other".to_string()]), None);
    }
}
