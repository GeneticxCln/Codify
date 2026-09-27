fn main() {
    // Re-run when the *frontend* changes, not just when Rust does.
    //
    // `tauri::generate_context!` lives in `lib.rs` and embeds `frontendDist`
    // (../ui/dist) into the binary at compile time. Cargo tracks that by the
    // mtime of the crate that invokes the macro — and if only the .ts/.tsx files
    // change, lib.rs is untouched, so cargo considers the crate fresh and the
    // binary keeps serving the *previous* ui/dist. That is not a build that is
    // slow to notice; it is a build that silently ships the old frontend while
    // every source-level test passes against the new one.
    //
    // Emitting the dist directory as a build-script input is the fix: the
    // fingerprint covers the files, so a change to any of them re-runs the
    // script and forces lib.rs to be re-expanded with the new assets.
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-changed=tauri.conf.json");
    println!("cargo:rerun-if-changed=../ui/dist");
    tauri_build::build()
}
