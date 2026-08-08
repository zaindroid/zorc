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
