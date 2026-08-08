# Runbook

## Restore from backup

TODO

## Lost SSH access / server won't boot (HP EFI boot-order quirk)

**Known issue:** this HP ZBook 15 G2's firmware (BIOS M70 Ver. 01.07) periodically
drops the "Ubuntu" UEFI boot entry from `BootOrder` — the entry itself stays defined
in NVRAM (`efibootmgr -v` will show `Boot0004* Ubuntu` pointing at
`\EFI\ubuntu\shimx64.efi`), it just isn't included in the order the firmware
actually tries, so it falls through to legacy BBS-style entries ("Notebook Hard
Drive" etc.) that don't reliably chain into the real bootloader. Result: server
appears to hang / never comes back after a reboot.

**Fix (already automated as of 2026-08-08):** `fix-efi-bootorder.service`
(systemd oneshot, `bootstrap/05-fix-efi-bootorder.sh`) runs on every successful
boot and reasserts the Ubuntu entry first in `BootOrder`, plus keeps
`/boot/efi/EFI/BOOT/BOOTX64.EFI` (the UEFI-spec fallback path) in sync with the
real shim as a second line of defense. This means: if a boot succeeds, the next
one should self-correct even if the firmware regresses the order again.

**If it still fails to boot** (e.g. two bad boots in a row before the fix could
re-run, or a fresh/unknown regression): physical/console access needed —
HP F10 at boot for the boot device menu, manually select "Ubuntu" /
`\EFI\ubuntu\shimx64.efi`. Once booted, confirm the fix ran:
`systemctl status fix-efi-bootorder.service` and `sudo efibootmgr | grep BootOrder`
(Ubuntu's boot number should be first).

## An app eating memory

TODO

## EFI boot-order regression — observed pattern (2026-08-08)

First real-world test of `fix-efi-bootorder.service`: a `sudo init 0` (full
poweroff, not a warm reboot) required manual boot-menu intervention (F9 →
Boot from EFI File → `\EFI\ubuntu\shimx64.efi`) to get back up — the firmware
had dropped the Ubuntu entry from BootOrder again during the power-off/cold-boot
cycle. The self-healing service correctly re-fixed the order immediately on
that boot (confirmed via `journalctl -u fix-efi-bootorder.service`), but by
design it can only protect boots *after* a successful one — it cannot prevent
the specific boot where a regression first occurs, since nothing runs before
the firmware's own boot decision.

Working theory: this may be specifically tied to full power-off/cold-boot
cycles rather than warm reboots (`sudo reboot`) — old HP firmware NVRAM
persistence is often less reliable across a true power cycle than across an
ACPI-triggered restart within the same power session. Not yet confirmed with
a plain `sudo reboot` test. If true, prefer `sudo reboot` over `sudo init 0`/
`shutdown -h`/physical power cycling for routine restarts (e.g. after kernel
updates) to reduce the chance of landing in this state.

If it happens again: same recovery as before (F9 boot menu → EFI file →
ubuntu/shimx64.efi), then verify `sudo efibootmgr | grep BootOrder` shows 0004
(or whatever the Ubuntu entry's current number is) first, and
`systemctl status fix-efi-bootorder.service` shows a recent successful run.

## EFI boot-order fix — resolution (2026-08-08, later same day)

The original `fix-efi-bootorder.service` crashed the first time it hit a more
severe variant of the firmware bug: `BootOrder` wiped entirely (not just
misordered), which the script hadn't accounted for (`set -e` + no match on
`grep 'BootOrder:'` = hard crash). Root boot config was manually repaired at
the console: removed the old shim-based `Boot0004 "Ubuntu"` entry, created
`Boot0005 "Ubuntu GRUB"` pointing directly at `grubx64.efi` (no shim — fine,
Secure Boot is disabled on this system), and pointed the UEFI-spec fallback
path (`/boot/efi/EFI/BOOT/BOOTX64.EFI`) at that same `grubx64.efi`.

That fallback path turned out to be the more reliable recovery mechanism —
it's what actually got the system booting when `BootOrder` was wiped and the
firmware fell back to its own recovery scan. `05-fix-efi-bootorder.sh` was
rewritten accordingly: best-effort fixes the NVRAM entry/order when possible
(now tolerates a missing `BootOrder` instead of crashing), but *always*
re-syncs the fallback file to `grubx64.efi` regardless — that unconditional
step is the real safety net now, not the NVRAM order.
