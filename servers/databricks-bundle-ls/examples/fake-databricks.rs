//! A stand-in `databricks` CLI for the end-to-end tests, portable to Windows. It records its
//! arguments to `$FAKE_DATABRICKS_DIR/args.txt`, prints `stderr.txt` from that directory to
//! stderr and exits with the code in `exit.txt` (default 0).

use std::path::PathBuf;

fn main() {
    let dir = PathBuf::from(std::env::var_os("FAKE_DATABRICKS_DIR").expect("FAKE_DATABRICKS_DIR is not set"));
    let args: Vec<String> = std::env::args().skip(1).collect();
    std::fs::write(dir.join("args.txt"), args.join(" ")).unwrap();
    eprint!(
        "{}",
        std::fs::read_to_string(dir.join("stderr.txt")).unwrap_or_default()
    );
    let code = std::fs::read_to_string(dir.join("exit.txt")).map_or(0, |code| code.trim().parse().unwrap());
    std::process::exit(code);
}
