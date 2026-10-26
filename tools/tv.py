#!/usr/bin/env python3
"""Explicit-target access for the documented U8000F profile. See docs/02-access.md."""
import argparse
import asyncio
import base64
import gzip
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor"))
from samsung_tv_root.archive_root import probe, _check_target, _capture
from samsung_tv_root.sdb import SdbClient, find_sdb
sys.path.insert(0, str(ROOT / "tools"))
from plan_cleanup import FIELDS, make_shell, make_sql, read_rows

PROFILE = json.loads((ROOT / "profiles/un75u8000ffxza-1300.9.json").read_text())
STAGE = "/home/owner/share/tmp/sdk_tools/quiet-tizen"
CU7000_BACKUP_ROOT = "/opt/usr/share/quiet-tizen-backups/cu7000"
CU7000_CLEANUP_GROUPS = ("advertising",) + tuple(f"optional-apps-{i:02d}" for i in range(1, 12))

CU7000_LAUNCHER_PREFLIGHT = (
    ("Tizen framework assemblies", "for n in Tizen.NUI.dll Tizen.Applications.Common.dll Tizen.System.dll Tizen.dll; do "
     "echo ==$n==; find /usr/share/dotnet.tizen -type f -name \"$n\" -print 2>/dev/null; done"),
    ("Existing Home package layout", "for p in /opt/usr/apps/com.samsung.tv.bixby-promotion "
     "/opt/usr/apps/com.samsung.tv.quicklaunch /opt/usr/apps/com.samsung.tv.csfs; do if [ -e \"$p\" ]; then ls -ld \"$p\"; "
     "for f in bin/BixbyPromotion.dll bin/QuickLaunch.dll; do if [ -e \"$p/$f\" ]; then "
     "stat -c '%n owner=%u:%g mode=%a size=%s' \"$p/$f\"; fi; done; else echo \"$p absent\"; fi; done"),
    ("Relevant app package records", "sqlite3 -readonly -header -csv /opt/dbspace/.pkgmgr_parser.db \"SELECT a.app_id,a.package,a.app_component,a.app_exec,a.app_package_system,a.app_removable,p.package_version,p.root_path,p.package_system FROM package_app_info AS a LEFT JOIN package_info AS p ON p.package=a.package WHERE a.app_id IN ('com.samsung.tv.csfs','com.samsung.tv.bixby-promotion','com.samsung.tv.quicklaunch','DclHome001.Home','QuietHm001.Home','QuietBt001.Boot') ORDER BY a.app_id;\""),
    ("Quiet Home package record", "sqlite3 -readonly -header -csv /opt/dbspace/.pkgmgr_parser.db \"SELECT a.app_id,a.package,a.app_exec,p.package_version,p.root_path FROM package_app_info AS a LEFT JOIN package_info AS p ON p.package=a.package WHERE a.app_id='QuietHm001.Home';\""),
    ("Validated Home routing backup", "b=/opt/usr/share/quiet-tizen-backups/cu7000/quiet-home-routing-verified-v3.env; if [ -f \"$b\" ]; then cat \"$b\"; else echo 'No validated Home routing backup exists yet.'; fi"),
    ("Installed DclHome container files", "find /opt/usr/apps/DclHome001 -maxdepth 3 -type f -print 2>/dev/null | sort; "
     "echo '== system Home package =='; ls -la /usr/apps/com.samsung.tv.csfs 2>/dev/null | head -25"),
    ("Home routing and current HDMI source", "for key in db/appfw/home_appid db/appfw/startup_appid memory/system/source_type; do "
     "printf '%s: ' \"$key\"; vconftool get \"$key\" 2>&1 || true; done"),
)
CU7000_FRAMEWORK_FILES = (
    ("Tizen.NUI.dll", "/usr/share/dotnet.tizen/framework/Tizen.NUI.dll"),
    ("Tizen.Applications.Common.dll", "/usr/share/dotnet.tizen/framework/Tizen.Applications.Common.dll"),
    ("Tizen.System.dll", "/usr/share/dotnet.tizen/framework/Tizen.System.dll"),
    ("Tizen.dll", "/usr/share/dotnet.tizen/framework/Tizen.dll"),
)

CU7000_PROCESS_AUDIT = (
    ("load and memory", "echo '== uptime/load =='; cat /proc/uptime /proc/loadavg; "
     "echo '== memory =='; grep -E '^(MemTotal|MemFree|MemAvailable|Buffers|Cached|SwapTotal|SwapFree):' /proc/meminfo; "
     "echo '== process count =='; n=0; for p in /proc/[0-9]*; do test -d \"$p\" && n=$((n+1)); done; echo \"processes=$n\""),
    ("live processes", "if ps -eo pid,ppid,uid,stat,pcpu,pmem,comm --sort=-pcpu >/dev/null 2>&1; then "
     "ps -eo pid,ppid,uid,stat,pcpu,pmem,comm --sort=-pcpu | head -n 45; "
     "else ps; fi"),
    ("active system services", "systemctl list-units --type=service --state=running --no-pager 2>&1 | head -n 100"),
    ("Quiet TV apps and HDMI viewer processes", "ps -ef 2>/dev/null | grep -E 'Quiet(Hm|Bt)|tv-viewer|app_launcher' | grep -v grep || true"),
)

# Fixed, read-only CU7000 reconnaissance. Keep this separate from arbitrary
# root exec so the experimental profile cannot run cleanup or launcher scripts.
CU7000_INVENTORY = (
    (
        "platform",
        "echo '== Platform =='; uname -srmo; "
        "echo '== .NET runtime =='; readlink -f /usr/share/dotnet.tizen/netcoreapp; "
        "echo '== File capabilities =='; getcap /usr/bin/dotnet /usr/sbin/sdbd-tarlauncher; "
        "echo '== tar support =='; /usr/bin/tar --help | grep -- --to-command",
    ),
    (
        "databases",
        "echo '== Database files in /opt/dbspace =='; "
        "ls -ln /opt/dbspace/.[!.]*.db /opt/dbspace/*.db 2>/dev/null || true",
    ),
    (
        "package-registry-schema",
        "db=/opt/dbspace/.pkgmgr_parser.db; "
        "if [ -r \"$db\" ]; then "
        "sqlite3 -readonly \"$db\" \"SELECT sql FROM sqlite_master WHERE type='table' AND name='package_app_info';\"; "
        "else echo 'package registry not found at the documented path'; fi",
    ),
    (
        "package-table-schema",
        "db=/opt/dbspace/.pkgmgr_parser.db; "
        "if [ -r \"$db\" ]; then "
        "sqlite3 -readonly \"$db\" \"SELECT sql FROM sqlite_master WHERE type='table' AND name='package_info';\"; "
        "else echo 'package registry not found at the documented path'; fi",
    ),
    (
        "installed-package-ids",
        "db=/opt/dbspace/.pkgmgr_parser.db; "
        "if [ -r \"$db\" ] && sqlite3 -readonly \"$db\" \"SELECT 1 FROM sqlite_master WHERE type='table' AND name='package_app_info';\" | grep -q 1; then "
        "sqlite3 -readonly -header -csv \"$db\" \"SELECT app_id FROM package_app_info ORDER BY app_id;\"; "
        "else echo 'package_app_info table not found'; fi",
    ),
    (
        "optional-package-flags",
        "db=/opt/dbspace/.pkgmgr_parser.db; "
        "if [ -r \"$db\" ]; then sqlite3 -readonly -header -csv \"$db\" \""
        "SELECT app_id,app_disable,app_onboot,app_autorestart,app_removable,app_package_system,package "
        "FROM package_app_info WHERE lower(app_package_system) IN ('false','0') "
        "OR lower(app_removable) IN ('true','1') "
        "OR app_id IN ('com.samsung.tizen.samsung-analytics','com.samsung.tizen.smartthings-client',"
        "'com.samsung.tizen.smartthings-broker','com.samsung.tizen.smartthings-hub',"
        "'com.samsung.tv.acr','com.samsung.tv.adagent-service','com.samsung.tv.adagent-service-ut',"
        "'com.samsung.tv.canalysis-service','com.samsung.tv.contents-recognition-dbmgr',"
        "'com.samsung.tv.targeted-ad-service','com.samsung.tv.pisa-service','org.tizen.pisa-control-service',"
        "'content-grab-service','com.samsung.tv.activity-inference-service','com.samsung.tv.iacr',"
        "'com.samsung.tv.ad-web-framework','com.samsung.tv.adplayer','adplatform.ADPlayer',"
        "'com.samsung.tv.ondevice-voice','com.samsung.tv.farfieldvoice','com.samsung.tv.bixby-prov',"
        "'com.samsung.tv.alexa-client','com.samsung.tv.alexa-fullscreen-app',"
        "'com.samsung.tv.google-assistant','com.samsung.tv.google-fullscreen-app',"
        "'org.tizen.voice-app','org.tizen.voice-app-service','org.tizen.voice-client','org.tizen.voice-onboarding',"
        "'com.samsung.tv.home.sidebar','com.samsung.tv.tvplus','com.samsung.tv.tvPlusDeeplinker') "
        "OR app_id GLOB 'com.samsung.tv.bixby*' OR app_id GLOB 'com.samsung.tv.wakeup-engine-*' "
        "OR app_id GLOB 'com.samsung.tv.context-aware-*' OR app_id GLOB 'com.samsung.tv.smartthings*' "
        "OR app_id GLOB 'com.samsung.tv.iot-*' OR app_id GLOB 'org.tizen.netflix*' "
        "OR app_id GLOB 'org.tizen.primevideo*' ORDER BY app_id;\"; "
        "else echo 'package registry not found at the documented path'; fi",
    ),
    (
        "home-selectors",
        "for key in db/appfw/home_appid db/appfw/startup_appid; do "
        "printf '%s: ' \"$key\"; vconftool get \"$key\" 2>&1 || true; done",
    ),
    (
        "candidate-services",
        "find /usr/lib/systemd/system /lib/systemd/system /etc/systemd/system "
        "-maxdepth 3 -type f -name '*.service' -print 2>/dev/null | "
        "grep -Ei 'adagent|pisa|canalysis|voice|bixby|acr|analytics|recognition|context|tvplus|hub|promotion|smartthings' || true",
    ),
)


def private_path(path):
    path = Path(path).expanduser().resolve()
    private = (ROOT / ".local").resolve()
    if path == private or private not in path.parents:
        raise ValueError("Private files must be inside this checkout's .local/ directory.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def save_private(path, data):
    path = private_path(path)
    path.write_bytes(data)
    path.chmod(0o600)


def observed_model(client):
    client.connect()
    client.require_device()
    rows = [line.split() for line in client.run(("devices",), check=True).stdout.splitlines()]
    rows = [row for row in rows if row and row[0] == client.serial]
    if len(rows) != 1 or len(rows[0]) < 3:
        raise ValueError("Cannot identify the explicitly selected device from SDB.")
    return rows[0][-1]


def check_identity(model, firmware, profile=PROFILE):
    if model != profile["model"]:
        raise ValueError(f"Unsupported model {model!r}; stop and investigate a separate profile.")
    if firmware != profile["firmware"]:
        raise ValueError("Supply --firmware with the exact version read from About This TV. "
                         "Only the profile's exact build is accepted. Do not guess this value.")


def load_profile(path):
    profile_dir = (ROOT / "profiles").resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    candidate = candidate.resolve()
    if candidate.parent != profile_dir or candidate.suffix != ".json":
        raise ValueError("Select a JSON profile directly inside this checkout's profiles/ directory.")
    return json.loads(candidate.read_text())


def pull_command(remote):
    if not remote.startswith("/") or "\0" in remote or "\n" in remote:
        raise ValueError("Use an absolute, single-line TV file path.")
    # The upstream command channel captures at most 1 MiB per stream.
    # A compressed transfer is bounded before emitting any base64.
    return f"""set -eu
test -f {shlex.quote(remote)}
test "$(stat -c %s {shlex.quote(remote)})" -le 33554432
t=$(mktemp /run/quiet-tizen-pull.XXXXXX)
trap 'rm -f "$t"' EXIT HUP INT TERM
gzip -c {shlex.quote(remote)} > "$t"
test "$(wc -c < "$t")" -le 760000 || {{ echo 'File too large; split a snapshot into smaller files first.' >&2; exit 1; }}
sha256sum {shlex.quote(remote)} | cut -d ' ' -f 1
base64 "$t"
"""


def decode_pull(stdout):
    digest, encoded = stdout.split("\n", 1)
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Missing transfer digest.")
    compressed = base64.b64decode("".join(encoded.split()), validate=True)
    data = gzip.decompress(compressed)
    if len(data) > 33554432 or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("Transfer size or digest mismatch; file was not saved.")
    return data


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="The owner's TV IPv4 address; never auto-discovered")
    parser.add_argument("--firmware", help="Operator-verified version from the TV's About screen")
    parser.add_argument("--profile", help="Exact device profile in profiles/; alternate profiles can be action-limited")
    parser.add_argument("--sdb", help="Path to Samsung's separately installed SDB")
    parser.add_argument("--callback-host", help="This computer's LAN IPv4 if route detection is wrong")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("doctor", help="Read SDB identity and archive-route capabilities; does not obtain root")
    sub.add_parser("root-check", help="Temporarily obtain root and run id; no configuration changes")
    sub.add_parser("inventory", help="Run the fixed, read-only CU7000 inventory; no arbitrary commands")
    sub.add_parser("process-audit", help="Read live processes, CPU/memory, and active services; changes nothing")
    sub.add_parser("launcher-preflight", help="Read-only CU7000 launcher/runtime inventory; changes nothing")
    sub.add_parser("fetch-frameworks", help="Fetch the four matching TV framework references into private .local/")
    sub.add_parser("source-probe", help="Run the fixed read-only HDMI/Home probe and remove its staged files")
    sub.add_parser("activate-quiet-home", help="Back up Samsung Home routing, then set Quiet Home for Home/startup")
    sub.add_parser("restore-home", help="Restore the saved Samsung Home operation and startup selectors")
    sub.add_parser("set-hdmi1-startup", help="Back up the current startup selector, then boot through the HDMI 1 helper")
    sub.add_parser("restore-hdmi1-startup", help="Restore Quiet Home as startup while keeping it as the Home button app")
    sub.add_parser("install-quiet-home", help="Install and launch the signed HDMI-only Quiet Home app; leaves Samsung Home routing unchanged")
    snapshot = sub.add_parser("snapshot", help="Create or retrieve a consistent CU7000 package-registry backup")
    snapshot.add_argument("name", help="New private snapshot name; letters, digits, dot, underscore, hyphen")
    cleanup = sub.add_parser("apply-cleanup", help="Apply reviewed CU7000 optional-app plans with on-TV rollback")
    cleanup.add_argument("snapshot", help="Existing snapshot name used to generate the plan")
    cleanup.add_argument("group", nargs="+", choices=CU7000_CLEANUP_GROUPS, help="Reviewed cleanup group(s)")
    restore = sub.add_parser("restore-cleanup", help="Restore one previously applied CU7000 registry group")
    restore.add_argument("snapshot", help="Snapshot name used to generate the plan")
    restore.add_argument("group", choices=CU7000_CLEANUP_GROUPS, help="Reviewed cleanup group")
    run = sub.add_parser("exec", help="Execute one reviewed command or short script using temporary root")
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument("--command")
    source.add_argument("--script", type=Path)
    run.add_argument("--arg", action="append", default=[], help="Positional argument passed to --script")
    pull = sub.add_parser("pull", help="Read and verify one bounded file into ignored .local/")
    pull.add_argument("remote")
    pull.add_argument("local", type=Path)
    push = sub.add_parser("push", help="Upload one file to the SDK staging directory, not to system paths")
    push.add_argument("local", type=Path)
    push.add_argument("name", help="Simple staging filename")
    args = parser.parse_args()
    ipaddress.IPv4Address(args.host)
    if args.callback_host:
        ipaddress.IPv4Address(args.callback_host)
    if args.sdb:
        os.environ["SDB"] = str(Path(args.sdb).expanduser().resolve())
    client = SdbClient(find_sdb(), args.host, timeout=15)
    model = observed_model(client)
    print("SDB model:", model)
    if args.action == "doctor":
        assessment = _check_target(client)
        print("Archive-route runtime family:", assessment.runtime)
        print(assessment.capabilities)
        print(_capture(client, "uname -srmo; readlink -f /usr/share/dotnet.tizen/netcoreapp"))
        print("Preflight only. Firmware must be read on the TV; root is not yet confirmed.")
        return
    profile = load_profile(args.profile) if args.profile else PROFILE
    allowed_actions = profile.get("allowed_actions", ["root-check", "exec", "pull", "push"])
    if args.action not in allowed_actions:
        raise ValueError(f"Action {args.action!r} is not enabled by profile {profile['model']!r}.")
    check_identity(model, args.firmware, profile)
    if args.action == "push":
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", args.name):
            raise ValueError("Use a simple staging filename without directory components.")
        if not args.local.is_file():
            raise ValueError("Upload source must be a file.")
        _capture(client, f"mkdir -p {STAGE}")
        remote = PurePosixPath(STAGE) / args.name
        client.push(args.local.resolve(), remote)
        digest = hashlib.sha256(args.local.read_bytes()).hexdigest()
        actual = _capture(client, f"sha256sum {shlex.quote(str(remote))}").split()[0]
        if digest != actual:
            raise ValueError("Uploaded file digest mismatch.")
        print(f"Verified staging file: {remote}. Remove it after deployment.")
        return
    if args.action in ("activate-quiet-home", "restore-home", "set-hdmi1-startup", "restore-hdmi1-startup"):
        probe_dll = ROOT / ".local/build/Probe/Probe.dll"
        probe_config = ROOT / ".local/build/Probe/Probe.runtimeconfig.json"
        if not probe_dll.is_file() or not probe_config.is_file():
            raise ValueError("The fixed Probe build is missing from .local/build/Probe/.")
        local_backup = ROOT / ".local/snapshots/quiet-home-routing-original.txt"
        backup_text = (
            "main_home=com.samsung.tv.csfs\n"
            "home_appid=com.samsung.tv.csfs\n"
            "startup_appid=com.samsung.tv.csfs\n"
        )
        if local_backup.exists():
            if local_backup.read_text() != backup_text:
                raise ValueError("The private Home-routing backup exists and differs; inspect it before proceeding.")
        else:
            save_private(local_backup, backup_text.encode())
        remote_probe = PurePosixPath(STAGE) / "QuietHomeRouteProbe.dll"
        remote_config = PurePosixPath(STAGE) / "QuietHomeRouteProbe.runtimeconfig.json"
        _capture(client, f"mkdir -p {shlex.quote(STAGE)}")
        client.push(probe_dll, remote_probe)
        client.push(probe_config, remote_config)
        if args.action in ("set-hdmi1-startup", "restore-hdmi1-startup"):
            boot_app = "QuietBt001.Boot"
            remote_backup = CU7000_BACKUP_ROOT + "/quiet-home-startup-before-hdmi.env"
            local_startup_backup = ROOT / ".local/snapshots/quiet-home-startup-before-hdmi.txt"
            startup_text = "startup_appid=QuietHm001.Home\n"
            if args.action == "set-hdmi1-startup":
                if local_startup_backup.exists() and local_startup_backup.read_text() != startup_text:
                    raise ValueError("The private startup backup exists and differs; inspect it before proceeding.")
                if not local_startup_backup.exists():
                    save_private(local_startup_backup, startup_text.encode())
                action_lines = (
                    "set -eu; "
                    f"p={shlex.quote(str(remote_probe))}; c={shlex.quote(str(remote_config))}; b={shlex.quote(remote_backup)}; "
                    f"trap 'rm -f {shlex.quote(str(remote_probe))} {shlex.quote(str(remote_config))}' EXIT HUP INT TERM; "
                    "test \"$(sqlite3 -readonly /opt/dbspace/.pkgmgr_parser.db \"SELECT count(*) FROM package_app_info WHERE app_id='QuietBt001.Boot' AND package='QuietBt001';\")\" = 1; "
                    "vconftool get db/appfw/home_appid | grep -F 'value = QuietHm001.Home (String)' >/dev/null; "
                    "if [ ! -e \"$b\" ]; then vconftool get db/appfw/startup_appid | grep -F 'value = QuietHm001.Home (String)' >/dev/null; "
                    "mkdir -p \"$(dirname \"$b\")\"; (umask 077; echo 'startup_appid=QuietHm001.Home' > \"$b\"); fi; "
                    "grep -qx 'startup_appid=QuietHm001.Home' \"$b\"; "
                    "vconftool set -f -t string db/appfw/startup_appid QuietBt001.Boot; "
                    "vconftool get db/appfw/home_appid | grep -F 'value = QuietHm001.Home (String)' >/dev/null; "
                    "vconftool get db/appfw/startup_appid | grep -F 'value = QuietBt001.Boot (String)' >/dev/null; "
                    "echo 'Startup now selects HDMI 1 via Quiet Boot; Home button remains Quiet Home.'"
                )
            else:
                action_lines = (
                    "set -eu; "
                    f"p={shlex.quote(str(remote_probe))}; c={shlex.quote(str(remote_config))}; b={shlex.quote(remote_backup)}; "
                    f"trap 'rm -f {shlex.quote(str(remote_probe))} {shlex.quote(str(remote_config))}' EXIT HUP INT TERM; "
                    "test -s \"$b\"; grep -qx 'startup_appid=QuietHm001.Home' \"$b\"; "
                    "vconftool get db/appfw/startup_appid | grep -F 'value = QuietBt001.Boot (String)' >/dev/null; "
                    "vconftool set -f -t string db/appfw/startup_appid QuietHm001.Home; "
                    "vconftool get db/appfw/startup_appid | grep -F 'value = QuietHm001.Home (String)' >/dev/null; "
                    "echo 'Startup restored to Quiet Home; Home button remains Quiet Home.'"
                )
            result = asyncio.run(probe(
                args.host, model=model, callback_host=args.callback_host,
                payloads=ROOT / ".local/build/payloads", commands=(action_lines,), command_timeout=60,
            ))
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            log = ROOT / ".local/sessions" / f"{stamp}.json"
            save_private(log, (json.dumps(result, indent=2) + "\n").encode())
            if result.get("uid") != 0 or result.get("euid") != 0 or not result.get("system_files_unchanged") or not result.get("staging_cleaned"):
                raise ValueError("Startup routing root-session checks failed; inspect the private session log.")
            response = result["commands"][0]
            if response["stdout"]:
                print(response["stdout"], end="" if response["stdout"].endswith("\n") else "\n")
            if response["stderr"]:
                print(response["stderr"], file=sys.stderr, end="")
            if response["exit_code"] or response["timed_out"]:
                raise RuntimeError(f"Startup routing action failed ({response['exit_code']}, timeout={response['timed_out']}). Private log: {log.relative_to(ROOT)}")
            return
        if args.action == "activate-quiet-home":
            action_lines = (
                "set -eu; "
                f"p={shlex.quote(str(remote_probe))}; c={shlex.quote(str(remote_config))}; b={shlex.quote(CU7000_BACKUP_ROOT + '/quiet-home-routing-verified-v3.env')}; "
                f"trap 'rm -f {shlex.quote(str(remote_probe))} {shlex.quote(str(remote_config))}' EXIT HUP INT TERM; "
                "test \"$(sqlite3 -readonly /opt/dbspace/.pkgmgr_parser.db \"SELECT count(*) FROM package_app_info WHERE app_id='QuietHm001.Home' AND package='QuietHm001';\")\" = 1; "
                "test \"$(sqlite3 -readonly /opt/dbspace/.pkgmgr_parser.db \"SELECT package_version FROM package_info WHERE package='QuietHm001';\")\" = 1.0.2; "
                "if [ ! -e \"$b\" ]; then "
                "main=$(/usr/bin/dotnet \"$p\" | sed -n 's/^home_result=0 home=//p'); "
                "vconftool get db/appfw/home_appid | grep -F 'value = com.samsung.tv.csfs (String)' >/dev/null; h=com.samsung.tv.csfs; "
                "vconftool get db/appfw/startup_appid | grep -F 'value = com.samsung.tv.csfs (String)' >/dev/null; s=com.samsung.tv.csfs; "
                "test \"$main\" = com.samsung.tv.csfs && test \"$h\" = com.samsung.tv.csfs && test \"$s\" = com.samsung.tv.csfs; "
                "mkdir -p \"$(dirname \"$b\")\"; (umask 077; echo \"main_home=$main\" > \"$b\"; echo \"home_appid=$h\" >> \"$b\"; echo \"startup_appid=$s\" >> \"$b\"); fi; "
                "grep -qx 'main_home=com.samsung.tv.csfs' \"$b\"; grep -qx 'home_appid=com.samsung.tv.csfs' \"$b\"; grep -qx 'startup_appid=com.samsung.tv.csfs' \"$b\"; "
                "changed=0; rollback(){ if [ \"$changed\" = 1 ]; then /usr/bin/dotnet \"$p\" set-home com.samsung.tv.csfs >/dev/null 2>&1 || true; vconftool set -f -t string db/appfw/home_appid com.samsung.tv.csfs >/dev/null 2>&1 || true; vconftool set -f -t string db/appfw/startup_appid com.samsung.tv.csfs >/dev/null 2>&1 || true; fi; }; trap 'rollback; rm -f \"$p\" \"$c\"' EXIT HUP INT TERM; "
                "test \"$(/usr/bin/dotnet \"$p\" set-home QuietHm001.Home | sed -n '1p')\" = set_home=0; changed=1; "
                "vconftool set -f -t string db/appfw/home_appid QuietHm001.Home; "
                "vconftool set -f -t string db/appfw/startup_appid QuietHm001.Home; "
                "vconftool get db/appfw/home_appid | grep -F 'value = QuietHm001.Home (String)' >/dev/null; "
                "vconftool get db/appfw/startup_appid | grep -F 'value = QuietHm001.Home (String)' >/dev/null; "
                "test \"$(/usr/bin/dotnet \"$p\" | sed -n 's/^home_result=0 home=//p')\" = QuietHm001.Home; "
                "trap - EXIT HUP INT TERM; rm -f \"$p\" \"$c\"; "
                "echo 'Quiet Home is now the main-operation Home and startup app; Samsung Home routing is backed up.'"
            )
        else:
            action_lines = (
                "set -eu; "
                f"p={shlex.quote(str(remote_probe))}; b={shlex.quote(CU7000_BACKUP_ROOT + '/quiet-home-routing-verified-v3.env')}; "
                f"trap 'rm -f {shlex.quote(str(remote_probe))} {shlex.quote(str(remote_config))}' EXIT HUP INT TERM; "
                "test -s \"$b\"; grep -qx 'main_home=com.samsung.tv.csfs' \"$b\"; "
                "grep -qx 'home_appid=com.samsung.tv.csfs' \"$b\"; grep -qx 'startup_appid=com.samsung.tv.csfs' \"$b\"; "
                "test \"$(/usr/bin/dotnet \"$p\" set-home com.samsung.tv.csfs | sed -n '1p')\" = set_home=0; "
                "vconftool set -f -t string db/appfw/home_appid com.samsung.tv.csfs; "
                "vconftool set -f -t string db/appfw/startup_appid com.samsung.tv.csfs; "
                "vconftool get db/appfw/home_appid | grep -F 'value = com.samsung.tv.csfs (String)' >/dev/null; "
                "vconftool get db/appfw/startup_appid | grep -F 'value = com.samsung.tv.csfs (String)' >/dev/null; "
                "test \"$(/usr/bin/dotnet \"$p\" | sed -n 's/^home_result=0 home=//p')\" = com.samsung.tv.csfs; "
                "app_launcher -u 5001 -s com.samsung.tv.csfs; "
                "echo 'Restored Samsung Home and startup selectors from the saved exact-build routing backup.'"
            )
        result = asyncio.run(probe(
            args.host, model=model, callback_host=args.callback_host,
            payloads=ROOT / ".local/build/payloads", commands=(action_lines,), command_timeout=60,
        ))
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        log = ROOT / ".local/sessions" / f"{stamp}.json"
        save_private(log, (json.dumps(result, indent=2) + "\n").encode())
        if result.get("uid") != 0 or result.get("euid") != 0:
            raise ValueError("The callback did not prove UID/EUID 0; inspect the private session log.")
        if not result.get("system_files_unchanged") or not result.get("staging_cleaned"):
            raise ValueError("Root session cleanup checks failed; inspect the private session log.")
        response = result["commands"][0]
        if response["stdout"]:
            print(response["stdout"], end="" if response["stdout"].endswith("\n") else "\n")
        if response["stderr"]:
            print(response["stderr"], file=sys.stderr, end="")
        if response["exit_code"] or response["timed_out"]:
            raise RuntimeError(f"Home routing action failed ({response['exit_code']}, timeout={response['timed_out']}). "
                               f"Private log: {log.relative_to(ROOT)}")
        return
    if args.action == "root-check":
        commands = ("id",)
    elif args.action == "inventory":
        commands = tuple(command for _, command in CU7000_INVENTORY)
    elif args.action == "process-audit":
        commands = tuple(command for _, command in CU7000_PROCESS_AUDIT)
    elif args.action == "launcher-preflight":
        commands = tuple(command for _, command in CU7000_LAUNCHER_PREFLIGHT)
    elif args.action == "fetch-frameworks":
        commands = tuple(pull_command(remote) for _, remote in CU7000_FRAMEWORK_FILES)
    elif args.action == "source-probe":
        probe_dll = ROOT / ".local/build/Probe/Probe.dll"
        runtime_config = ROOT / ".local/build/Probe/Probe.runtimeconfig.json"
        if not probe_dll.is_file() or not runtime_config.is_file():
            raise ValueError("Build the fixed Probe first; expected .local/build/Probe/Probe.dll and runtimeconfig.")
        probe_remote = PurePosixPath(STAGE) / "QuietProbe.dll"
        config_remote = PurePosixPath(STAGE) / "QuietProbe.runtimeconfig.json"
        _capture(client, f"mkdir -p {shlex.quote(STAGE)}")
        _capture(client, f"test ! -e {shlex.quote(str(probe_remote))} && test ! -e {shlex.quote(str(config_remote))}")
        client.push(probe_dll, probe_remote)
        client.push(runtime_config, config_remote)
        commands = (f"set -eu; d={shlex.quote(str(probe_remote))}; c={shlex.quote(str(config_remote))}; "
                    "trap 'rm -f \"$d\" \"$c\"' EXIT HUP INT TERM; "
                    "test -s \"$d\"; test -s \"$c\"; /usr/bin/dotnet \"$d\"",)
    elif args.action == "snapshot":
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,40}", args.name):
            raise ValueError("Use a simple snapshot name of at most 41 characters.")
        remote_dir = f"{CU7000_BACKUP_ROOT}/{args.name}"
        remote_db = f"{remote_dir}/.pkgmgr_parser.db"
        remote_dir_q = shlex.quote(remote_dir)
        remote_db_q = shlex.quote(remote_db)
        backup = (
            "set -eu; source=/opt/dbspace/.pkgmgr_parser.db; "
            f"dest={remote_db_q}; dir={remote_dir_q}; "
            "if [ ! -f \"$dest\" ]; then "
            "test ! -e \"$dir\"; mkdir -p " + shlex.quote(CU7000_BACKUP_ROOT) + "; "
            "mkdir \"$dir\"; "
            "sqlite3 -bail \"$source\" \".backup '$dest'\"; "
            "fi; test \"$(sqlite3 -readonly \"$dest\" 'PRAGMA quick_check;')\" = ok; "
            "sha256sum \"$dest\""
        )
        commands = (backup, pull_command(remote_db))
    elif args.action == "apply-cleanup":
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,40}", args.snapshot):
            raise ValueError("Use a simple existing snapshot name of at most 41 characters.")
        snap = ROOT / ".local/snapshots" / f"{args.snapshot}-registry.db"
        remote_dir = f"{CU7000_BACKUP_ROOT}/{args.snapshot}"
        if not snap.is_file() or len(args.group) != len(set(args.group)):
            raise ValueError("Fresh snapshot is required and group names must not be repeated.")
        snapshot_digest = hashlib.sha256(snap.read_bytes()).hexdigest()
        commands = []
        for group in args.group:
            plan = ROOT / ".local/plans" / f"{args.snapshot}-{group}"
            if not plan.is_dir() or group not in profile.get("groups", {}):
                raise ValueError(f"Snapshot plan for {group} is missing or not enabled in this profile.")
            rows = read_rows(snap, profile["groups"][group]["app_ids"], profile.get("require_optional_packages", False))
            expected_apply = make_sql(rows)
            expected_restore = make_sql(rows, restore=True)
            apply_script = make_shell(expected_apply).encode()
            restore_script = make_shell(expected_restore).encode()
            if (plan / "apply.sql").read_text() != expected_apply or (plan / "restore.sql").read_text() != expected_restore:
                raise ValueError(f"Plan for {group} does not match the selected snapshot/profile.")
            if (plan / "apply.sh").read_bytes() != apply_script or (plan / "restore.sh").read_bytes() != restore_script:
                raise ValueError(f"Shell wrapper for {group} differs from reviewed SQL.")
            if len(apply_script) + len(restore_script) > 45000:
                raise ValueError(f"Reviewed cleanup scripts for {group} exceed the bounded channel.")
            remote_restore = f"{remote_dir}/restore-{group}.sh"
            restore_b64 = base64.b64encode(restore_script).decode()
            apply_b64 = base64.b64encode(apply_script).decode()
            restore_digest = hashlib.sha256(restore_script).hexdigest()
            commands.append(
                "set -eu; "
                f"test -f {shlex.quote(remote_dir + '/.pkgmgr_parser.db')}; "
                f"test \"$(sha256sum {shlex.quote(remote_dir + '/.pkgmgr_parser.db')} | cut -d ' ' -f 1)\" = {shlex.quote(snapshot_digest)}; "
                f"if [ -e {shlex.quote(remote_restore)} ]; then "
                f"test \"$(sha256sum {shlex.quote(remote_restore)} | cut -d ' ' -f 1)\" = {shlex.quote(restore_digest)}; "
                "else "
                f"printf '%s' {shlex.quote(restore_b64)} | base64 -d > {shlex.quote(remote_restore)}; "
                f"chmod 600 {shlex.quote(remote_restore)}; fi; "
                f"printf '%s' {shlex.quote(apply_b64)} | base64 -d | /bin/sh -s"
            )
        commands = tuple(commands)
    elif args.action == "restore-cleanup":
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,40}", args.snapshot):
            raise ValueError("Use a simple existing snapshot name of at most 41 characters.")
        snap = ROOT / ".local/snapshots" / f"{args.snapshot}-registry.db"
        plan = ROOT / ".local/plans" / f"{args.snapshot}-{args.group}"
        if not snap.is_file() or not plan.is_dir():
            raise ValueError("Expected snapshot and generated plan are missing from .local/.")
        if args.group not in profile.get("groups", {}):
            raise ValueError("This group is not present in the selected exact-model profile.")
        rows = read_rows(snap, profile["groups"][args.group]["app_ids"],
                         profile.get("require_optional_packages", False))
        expected_restore = make_sql(rows, restore=True)
        restore_script = make_shell(expected_restore).encode()
        if (plan / "restore.sql").read_text() != expected_restore or (plan / "restore.sh").read_bytes() != restore_script:
            raise ValueError("Restore plan differs from the selected snapshot/profile; regenerate and review it.")
        remote_restore = f"{CU7000_BACKUP_ROOT}/{args.snapshot}/restore-{args.group}.sh"
        digest = hashlib.sha256(restore_script).hexdigest()
        commands = (
            "set -eu; "
            f"test \"$(sha256sum {shlex.quote(remote_restore)} | cut -d ' ' -f 1)\" = {shlex.quote(digest)}; "
            f"/bin/sh {shlex.quote(remote_restore)}",
        )
    elif args.action == "pull":
        private_path(args.local)
        commands = (pull_command(args.remote),)
    elif args.script:
        script = args.script.read_bytes()
        if len(script) > 45000:
            raise ValueError("Script too large for the bounded command channel; split into phases.")
        encoded = base64.b64encode(script).decode()
        commands = (f"printf '%s' {shlex.quote(encoded)} | /bin/sh -s -- " + shlex.join(args.arg),)
    else:
        if args.arg:
            raise ValueError("--arg requires --script.")
        commands = (args.command,)
    result = asyncio.run(probe(
        args.host, model=model, callback_host=args.callback_host,
        payloads=ROOT / ".local/build/payloads", commands=commands, command_timeout=60,
    ))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    log = ROOT / ".local/sessions" / f"{stamp}.json"
    save_private(log, (json.dumps(result, indent=2) + "\n").encode())
    if result.get("uid") != 0 or result.get("euid") != 0:
        raise ValueError("The callback did not prove UID/EUID 0.")
    if not result.get("system_files_unchanged") or not result.get("staging_cleaned"):
        raise ValueError("Root session cleanup checks failed; inspect the private session log.")
    if args.action == "inventory":
        for (label, _), response in zip(CU7000_INVENTORY, result["commands"]):
            print(f"\n=== {label} (exit {response['exit_code']}, timeout={response['timed_out']}) ===")
            if response["stdout"]:
                print(response["stdout"], end="" if response["stdout"].endswith("\n") else "\n")
            if response["stderr"]:
                print(response["stderr"], file=sys.stderr, end="")
        print(f"Private inventory log: {log.relative_to(ROOT)}")
    elif args.action == "process-audit":
        for (label, _), response in zip(CU7000_PROCESS_AUDIT, result["commands"]):
            print(f"\n=== {label} (exit {response['exit_code']}, timeout={response['timed_out']}) ===")
            if response["stdout"]:
                print(response["stdout"], end="" if response["stdout"].endswith("\n") else "\n")
            if response["stderr"]:
                print(response["stderr"], file=sys.stderr, end="")
        print(f"Private process audit log: {log.relative_to(ROOT)}")
    elif args.action == "launcher-preflight":
        for (label, _), response in zip(CU7000_LAUNCHER_PREFLIGHT, result["commands"]):
            print(f"\n=== {label} (exit {response['exit_code']}, timeout={response['timed_out']}) ===")
            if response["stdout"]:
                print(response["stdout"], end="" if response["stdout"].endswith("\n") else "\n")
            if response["stderr"]:
                print(response["stderr"], file=sys.stderr, end="")
        print(f"Private preflight log: {log.relative_to(ROOT)}")
    elif args.action == "fetch-frameworks":
        destination = ROOT / ".local/tv-framework"
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        for (name, _), response in zip(CU7000_FRAMEWORK_FILES, result["commands"]):
            if response["exit_code"] or response["timed_out"]:
                raise RuntimeError(f"Failed to retrieve {name}; inspect private session log {log.relative_to(ROOT)}")
            data = decode_pull(response["stdout"])
            save_private(destination / name, data)
            print(f"Saved private TV reference .local/tv-framework/{name} ({len(data)} bytes)")
        print("These Samsung/Tizen reference assemblies are local build inputs; do not redistribute them.")
    elif args.action == "snapshot":
        backup_result, transfer_result = result["commands"]
        if backup_result["exit_code"] or backup_result["timed_out"]:
            raise RuntimeError(f"Backup/check failed. Private log: {log.relative_to(ROOT)}")
        if transfer_result["exit_code"] or transfer_result["timed_out"]:
            raise RuntimeError(f"Backup transfer failed; on-TV backup was preserved at "
                               f"{CU7000_BACKUP_ROOT}/{args.name}/.pkgmgr_parser.db. "
                               f"Private log: {log.relative_to(ROOT)}")
        local_snapshot = ROOT / ".local/snapshots" / f"{args.name}-registry.db"
        if local_snapshot.exists():
            raise ValueError(f"Refusing to overwrite local snapshot {local_snapshot.relative_to(ROOT)}.")
        data = decode_pull(transfer_result["stdout"])
        save_private(local_snapshot, data)
        print(f"On-TV backup: {CU7000_BACKUP_ROOT}/{args.name}/.pkgmgr_parser.db")
        print(f"Verified local snapshot: {local_snapshot.relative_to(ROOT)} ({len(data)} bytes)")
    elif args.action in ("apply-cleanup", "restore-cleanup"):
        groups = args.group if args.action == "apply-cleanup" else [args.group]
        if len(result["commands"]) != len(groups):
            raise RuntimeError(f"Expected {len(groups)} cleanup results, got {len(result['commands'])}. "
                               f"Private log: {log.relative_to(ROOT)}")
        for group, response in zip(groups, result["commands"]):
            print(f"\n=== {group} (exit {response['exit_code']}, timeout={response['timed_out']}) ===")
            if response["stdout"]:
                print(response["stdout"], end="" if response["stdout"].endswith("\n") else "\n")
            if response["stderr"]:
                print(response["stderr"], file=sys.stderr, end="")
            if response["exit_code"] or response["timed_out"]:
                raise RuntimeError(f"Cleanup group {group} failed ({response['exit_code']}, timeout={response['timed_out']}). "
                                   f"Private log: {log.relative_to(ROOT)}")
    else:
        response = result["commands"][0]
        if args.action == "source-probe" and response["stdout"]:
            print(response["stdout"], end="" if response["stdout"].endswith("\n") else "\n")
        if response["stderr"]:
            print(response["stderr"], file=sys.stderr, end="")
        if response["exit_code"] or response["timed_out"]:
            raise RuntimeError(f"Command failed ({response['exit_code']}, timeout={response['timed_out']}). "
                               f"Private log: {log.relative_to(ROOT)}")
    if args.action == "pull":
        response = result["commands"][0]
        save_private(args.local, decode_pull(response["stdout"]))
        print("Verified file saved inside .local/.")
    elif args.action == "root-check":
        response = result["commands"][0]
        print(response["stdout"], end="")
        print("UID/EUID 0 verified; root staging cleaned; passwd and SDB key hashes unchanged.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as error:
        print(f"Stopped: {error}", file=sys.stderr)
        sys.exit(1)
