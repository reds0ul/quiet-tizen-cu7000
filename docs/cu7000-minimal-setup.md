# CU7000 HDMI-only cleanup

This guide is for the exact profile `UN65CU7000BXZA` on firmware
`T-KSU2ECAKUC-2000.2`. The root route, custom Home/startup path, HDMI 1
selection, and reversible registry cleanup were exercised on one owner's TV.
Another set with a different model code or firmware is not covered by this
profile. A successful root check alone does not establish compatibility.

The cleanup disables **210 optional app records** that were marked removable
and non-system in the original TV's registry snapshot. It sets each selected
app's `app_disable` flag and clears `app_onboot` and `app_autorestart`. The
groups include installed streaming and other optional apps. Review the plans
before applying them if you want to keep any optional app. The operation does
not uninstall packages, remove system packages, or disable systemd services.
The TV still runs its Tizen services for HDMI, display, audio, remote/input,
networking, and application management. Existing app processes can remain
until they exit or the TV restarts.

This app cleanup is independent of Quiet Home and Quiet Boot. It does not
install either app or change the TV's Home/startup selectors. See
[launcher setup](04-launcher.md) for those separate steps and
[recovery](09-recovery.md) for restoring the original settings.

## Requirements

- Exact model and firmware shown above.
- Tizen Studio's SDB at `$HOME/tizen-studio/tools/sdb`.
- The .NET 6 SDK and Python 3.12 or newer on the computer.
- Developer Mode enabled on the TV, with its **Host PC IP** set to the
  computer's current LAN address. The TV and computer must be on the same
  network.
- A fresh, consistent package-registry snapshot created by this tool.

Keep your TV's IP local to your setup. In the examples below, replace
`TV_IP` and `HOST_IP` with addresses on your own LAN; do not publish them.

## Check and back up

From the repository root, build the access payloads and verify that the exact
profile is selected:

```sh
python3 tools/build.py payloads
TV_IP=192.168.1.50
HOST_IP=192.168.1.20
SDB="$HOME/tizen-studio/tools/sdb"
PROFILE=profiles/un65cu7000bxza-2000.2.json
FIRMWARE=T-KSU2ECAKUC-2000.2

python3 tools/tv.py --sdb "$SDB" --host "$TV_IP" \
  --callback-host "$HOST_IP" --firmware "$FIRMWARE" \
  --profile "$PROFILE" doctor
python3 tools/tv.py --sdb "$SDB" --host "$TV_IP" \
  --callback-host "$HOST_IP" --firmware "$FIRMWARE" \
  --profile "$PROFILE" root-check
```

If the model, firmware, Developer Mode host address, or root check does not
match, stop. Do not substitute the U8000F profile or enable arbitrary root
commands. Take a new snapshot before each cleanup phase:

```sh
SNAPSHOT=cu7000-minimal-01
python3 tools/tv.py --sdb "$SDB" --host "$TV_IP" \
  --callback-host "$HOST_IP" --firmware "$FIRMWARE" \
  --profile "$PROFILE" snapshot "$SNAPSHOT"
```

The tool saves the backup on the TV under
`/opt/usr/share/quiet-tizen-backups/cu7000/$SNAPSHOT/` and verifies a local
copy under `.local/snapshots/`. Keep both. `.local/` is ignored by Git and
contains private per-TV data.

## Generate and inspect the plans

Generate one plan for each batch. Each generated apply/restore pair is tied to
the new snapshot and exact profile:

```sh
for n in $(seq -w 1 11); do
  group="optional-apps-$n"
  python3 tools/plan_cleanup.py \
    --snapshot ".local/snapshots/$SNAPSHOT-registry.db" \
    --groups "$group" \
    --output ".local/plans/$SNAPSHOT-$group" \
    --profile "$PROFILE" || break
done
```

Review every `apply.sql` and `restore.sql` before proceeding. Confirm the app
IDs are apps you are willing to disable. The planner refuses a missing,
duplicate, system, or non-removable target. It also saves each row's original
flags so restore returns to that snapshot's state.

## Apply and verify

Only after reviewing all eleven plans, apply the batches:

```sh
python3 tools/tv.py --sdb "$SDB" --host "$TV_IP" \
  --callback-host "$HOST_IP" --firmware "$FIRMWARE" \
  --profile "$PROFILE" apply-cleanup "$SNAPSHOT" \
  optional-apps-01 optional-apps-02 optional-apps-03 \
  optional-apps-04 optional-apps-05 optional-apps-06 \
  optional-apps-07 optional-apps-08 optional-apps-09 \
  optional-apps-10 optional-apps-11
```

The tool checks each batch result and stores its restore script next to the
on-TV snapshot before changing registry flags. If a batch fails, stop and
inspect the private session log in `.local/sessions/`; restore any batches
that already completed. After all batches succeed, restart the TV to let
already-running optional apps exit. Check HDMI 1, Home, remote navigation,
volume, mute and audio after restart.

## Restore

Restore one batch at a time. Use the same snapshot name and profile used to
generate the plans:

```sh
for n in $(seq -w 1 11); do
  python3 tools/tv.py --sdb "$SDB" --host "$TV_IP" \
    --callback-host "$HOST_IP" --firmware "$FIRMWARE" \
    --profile "$PROFILE" restore-cleanup "$SNAPSHOT" \
    "optional-apps-$n" || break
done
```

The restore scripts verify their saved digest and restore only the three app
flags captured in the snapshot. Restart the TV after restoring if you want
re-enabled apps to launch again.

## Known limits

- This cleanup does not reduce the TV to only Quiet Home, Quiet Boot, and TV
  Viewer processes. Tizen needs many system daemons to operate the panel,
  HDMI, audio, and remote.
- It does not disable systemd units such as AirPlay, HbbTV, SmartThings,
  Samsung analytics or content-recognition services. This profile avoids
  broad service shutdowns because dependencies vary by firmware and can break
  core TV behavior.
- The 210 app entries came from one TV's snapshot. A different installation
  may have different records; the fresh snapshot and optional-package guards
  are required.
- Samsung firmware updates may change registry records or restore defaults.
  Re-run inventory and generate new plans from a fresh snapshot before making
  further changes.
