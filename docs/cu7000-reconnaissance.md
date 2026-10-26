# CU7000 reconnaissance

This fork adds an experimental profile for one Samsung `UN65CU7000BXZA` on
firmware `T-KSU2ECAKUC-2000.2`. It is a documented case study, not a
general-purpose installer or a claim that every CU7000 firmware behaves the
same way.

## Observed target

| Field | Observed value |
|---|---|
| Model | `UN65CU7000BXZA` |
| Firmware | `T-KSU2ECAKUC-2000.2` |
| OS | Tizen 7.0, reported by SDB |
| CPU architecture | 32-bit ARMv7, reported by SDB |
| SDB root capabilities | `sdbd_rootperm=disabled`; `rootonoff_support=disabled` |
| Access-route preflight | Passed on this set: `.NET 6.0.9`, Linux `5.4.77 armv7l`, expected `dotnet`/`sdbd-tarlauncher` capabilities, and tar `--to-command` support |
| User goal | HDMI-only use with Quiet Home and Quiet Boot |

The temporary root route authenticated UID/EUID 0 on this exact build. The
temporary staging was cleaned and checks confirmed `/etc/passwd` and Samsung's
SDB public-key hashes were unchanged. This does not make arbitrary root access
available: the profile enables only fixed, model-scoped actions.

## What was exercised

- A consistent package-registry snapshot was saved locally and on the TV.
- A reviewed advertising group was applied earlier in the process.
- The CU7000 optional-app profile contains 210 additional app records, split
  into eleven batches. Each target and parent package must be marked
  removable and non-system in the fresh snapshot. The cleanup changes only
  `app_disable`, `app_onboot`, and `app_autorestart`, with a saved inverse.
- Quiet Home and Quiet Boot were installed. Home routing and HDMI 1 selection
  were verified on the set; the Boot helper starts on HDMI 1 and launches the
  system TV Viewer.
- The AeroTizen dashboard that had been installed during setup was removed.

These observations are from one TV. They are not a controlled performance
benchmark and do not show that every optional service is safe to disable.

## What the cleanup does not do

The registry cleanup does not uninstall packages, edit system package files,
or disable systemd units. It does not shut down Tizen's display, HDMI, audio,
input, network, or application-management stack. The system service inventory
contained many active units; this profile intentionally does not turn them
off as a group. See [CU7000 HDMI-only cleanup](cu7000-minimal-setup.md) for
the reproducible steps and limits.

The upstream profile documents a different target,
`UN75U8000FFXZA` on `T-KSU2EFAKUC-0090-1300.9`. Its launcher assumptions,
cleanup groups, service markers and private framework assemblies must not be
used as evidence for this CU7000.
