"""Run the Horizon #17863 production-worker proof on a disposable native Windows runner."""
import argparse
import contextlib
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import string
import subprocess
import tempfile


GIB = 1024**3


def require(condition, detail):
    if not condition:
        raise RuntimeError(detail)


def run(args, cwd, env, timeout=180):
    args = list(map(str, args))
    print("+", subprocess.list2cmdline(args), flush=True)
    result = subprocess.run(args, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout)
    out = result.stdout.decode("utf-8", errors="replace")
    err = result.stderr.decode("utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(f"exit {result.returncode}\n{out[-4000:]}\n{err[-4000:]}")
    return out.strip()


def snapshot(root):
    records = {}
    for path in root.rglob("*"):
        if path.is_file():
            with path.open("rb") as stream:
                records[path.relative_to(root)] = hashlib.file_digest(stream, "sha256").hexdigest()
    return records


def equal_files(actual, expected, context):
    if actual != expected:
        changed = sorted(str(p) for p in actual.keys() | expected.keys() if actual.get(p) != expected.get(p))
        raise RuntimeError(f"{context}: {len(changed)} files differ: {changed[:15]}")


def safe(path):
    return str(path).isascii() and not any(c.isspace() for c in str(path))


def volumes():
    win = ctypes.windll.kernel32
    seen = set()
    fixed = []
    for letter in string.ascii_uppercase:
        path = Path(letter + ":\\")
        if win.GetDriveTypeW(str(path)) != 3:
            continue
        guid = ctypes.create_unicode_buffer(260)
        if not win.GetVolumeNameForVolumeMountPointW(str(path), guid, len(guid)) or guid.value in seen:
            continue
        seen.add(guid.value)
        fixed.append(path)
    return sorted(fixed, key=lambda p: shutil.disk_usage(p).free, reverse=True)


def reserve(volume, stack):
    try:
        path = stack.enter_context(tempfile.TemporaryDirectory(prefix="hp", dir=volume))
    except PermissionError:
        return None
    root = Path(path).resolve()
    require(safe(root), f"expected short ASCII scratch root: {root}")
    return root


def compiler_check(exe, roots, workspace, environment, index):
    status = json.loads(run([exe, "--json", "rust", "status"], workspace, environment))
    require(status.get("installed") and status.get("version"), f"fresh CLI reports {status}")
    installation = Path(status["installed"]).resolve()
    require(installation.is_relative_to(roots["build"]) and safe(installation), str(installation))
    rustbin = installation / "bin"
    arm = status["host"] == "aarch64-pc-windows-gnullvm"
    mingw = installation / ("llvm-mingw" if arm else "mingw64") / "bin"
    windres = mingw / ("aarch64-w64-mingw32-windres.exe" if arm else "windres.exe")
    require(windres.is_file(), f"managed windres missing: {windres}")
    env = {k: v for k, v in environment.items()
           if not k.startswith(("CARGO_", "RUST", "CC_", "CXX_"))}
    env.update(PATH=os.pathsep.join(map(str, [rustbin, mingw, env["PATH"]])),
               CARGO_HOME=str(roots["build"] / "cargo"), RUSTC=str(rustbin / "rustc.exe"),
               CARGO_NET_OFFLINE="true", CARGO_INCREMENTAL="0")
    if arm:
        env["CARGO_TARGET_AARCH64_PC_WINDOWS_GNULLVM_LINKER"] = str(mingw / "aarch64-w64-mingw32-clang.exe")
    sysroot = Path(run([env["RUSTC"], "--print", "sysroot"], workspace, env)).resolve()
    require(sysroot == installation, f"physical sysroot {sysroot}, installed {installation}")
    out = workspace / f"t{index}"
    run([rustbin / "cargo.exe", "build", "--offline", "--target-dir", out], workspace, env)
    require(run([out / "debug" / "relocated.exe"], workspace, env) == "relocated-ok", "offline program output")
    resource = out / "probe.o"
    run([windres, "-I", roots["build"] / "include", "-i", workspace / "probe.rc",
         "-O", "coff", "-o", resource], workspace, env)
    require(resource.stat().st_size > 0, "empty resource object")
    print("PASS restarted CLI, managed sysroot, offline Cargo program and windres:", installation, flush=True)


def smoke(exe, base, second_candidates, stack):
    source = {k: base / "s" / s for k, s in [("launcher", "l"), ("game", "g"), ("build", "b")]}
    for path in source.values():
        path.mkdir(parents=True)
    pointer = base / "location.json"
    pointer.write_text(json.dumps({"version": 1, "roots": {k: str(v) for k, v in source.items()}}), "utf-8")
    workspace = base / "w"
    workspace.mkdir()
    (workspace / "src").mkdir()
    (workspace / "Cargo.toml").write_text('[package]\nname="relocated"\nversion="0.1.0"\nedition="2021"\n', "utf-8")
    (workspace / "src/main.rs").write_text('fn main() { println!("relocated-ok"); }\n', "utf-8")
    (workspace / "probe.rc").write_text('#include "hprobe.h"\nPROBE_ID RCDATA { 1, 2, 3 }\n', "ascii")
    (source["build"] / "include").mkdir()
    (source["build"] / "include/hprobe.h").write_text("#define PROBE_ID 51\n", "ascii")
    (source["game"] / "saves").mkdir()
    (source["game"] / "saves/level.dat").write_bytes(bytes(range(256)) * 10)
    (source["launcher"] / "background.png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    settings = {"minecraft_dir": str(source["game"]), "background": str(source["launcher"] / "background.png")}
    (source["launcher"] / "settings.json").write_text(json.dumps(settings), "utf-8")

    env = {k: v for k, v in os.environ.items() if not k.startswith("HORIZON_")}
    for key, name in [("HORIZON_LAUNCHER_TRASH", "trash"), ("HORIZON_LAUNCHER_CLIPBOARD", "clipboard"),
                      ("HORIZON_LAUNCHER_DESKTOP", "desktop"), ("XDG_CONFIG_HOME", "config"),
                      ("XDG_DATA_HOME", "data"), ("TEMP", "tmp"), ("TMP", "tmp"), ("TMPDIR", "tmp")]:
        folder = base / name
        folder.mkdir(exist_ok=True)
        env[key] = str(folder)
    env.update(HORIZON_LAUNCHER_LOCATION_FILE=str(pointer), HORIZON_LAUNCHER_NO_SELF_UPDATE="1",
               HORIZON_TELEMETRY_URL="off", HORIZON_NO_AUDIO="1")

    run([exe, "rust", "install"], workspace, env, timeout=1500)
    original = {k: snapshot(p) for k, p in source.items()}
    size = sum(p.stat().st_size for root in source.values() for p in root.rglob("*") if p.is_file())
    print(f"original tree: {sum(map(len, original.values()))} files, {size:,} bytes", flush=True)
    require(shutil.disk_usage(base).free >= 2 * size + GIB, "insufficient space for two verified copies")
    unicode_target = base / "Users" / "Ира" / "My Games"
    unicode_target.mkdir(parents=True)
    targets = [(unicode_target, base), (base / "ascii", base)]
    for volume in second_candidates:
        if shutil.disk_usage(volume).free < size + GIB:
            continue
        other = reserve(volume, stack)
        if other:
            targets.append((other / "Игры 2", other))
            break
    if len(targets) < 3:
        print("SKIP cross-volume: no second distinct writable fixed volume with space for a copy.", flush=True)

    for index, (target, owner) in enumerate(targets):
        run([exe, "--relocate-data", target], workspace, env, timeout=1800)
        state = json.loads(pointer.read_text("utf-8"))
        require(state["version"] == 1, "pointer schema changed")
        roots = {k: Path(v).resolve(strict=True) for k, v in state["roots"].items()}
        require(roots["launcher"] == target / "launcher" and roots["game"] == target / "game", str(roots))
        require(roots["build"].is_relative_to(owner) and safe(roots["build"]), f"build outside isolated volume: {roots}")
        if safe(target):
            require(roots["build"] == target / "build", "build did not rejoin plain destination")
        else:
            require(not roots["build"].is_relative_to(target), "expected separate physical build")
        for k, path in source.items():
            equal_files(snapshot(path), original[k], "original " + k)
        for k in ["game", "launcher"]:
            actual, expected = snapshot(roots[k]), original[k].copy()
            if k == "launcher":
                actual.pop(Path("settings.json"))
                expected.pop(Path("settings.json"))
            equal_files(actual, expected, "moved " + k)
        # Cargo can create lock/cache metadata while validating the previous moved build.
        # All original managed toolchain and cache files must still match after each move.
        moved_build = snapshot(roots["build"])
        equal_files({p: moved_build.get(p) for p in original["build"]}, original["build"], "moved build")
        remapped = json.loads((roots["launcher"] / "settings.json").read_text("utf-8"))
        require(Path(remapped["minecraft_dir"]) == roots["game"], "settings game path did not move")
        require(Path(remapped["background"]) == roots["launcher"] / "background.png", "background did not move")
        compiler_check(exe, roots, workspace, env, index)
        print("PASS production move, original SHA-256 manifests and rewritten settings:", target, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("launcher", type=Path, help="compiled native horizon-launcher.exe")
    exe = parser.parse_args().launcher.resolve(strict=True)
    require(os.name == "nt", "this smoke needs native Windows")
    disks = volumes()
    with contextlib.ExitStack() as stack:
        for volume in disks:
            if shutil.disk_usage(volume).free < 7 * GIB:
                continue
            root = reserve(volume, stack)
            if root:
                smoke(exe, root, [v for v in disks if v != volume], stack)
                return
    raise RuntimeError("no writable short fixed-volume scratch root with 7 GiB free for managed toolchain")


if __name__ == "__main__":
    main()
