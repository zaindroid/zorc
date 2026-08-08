#!/bin/bash
# HP ZBook firmware quirk workaround. Originally: the UEFI NVRAM 'Ubuntu'
# boot entry periodically got dropped from BootOrder (though the entry
# itself stayed defined). Observed 2026-08-08: a more severe variant where
# the BootOrder variable itself was wiped entirely (efibootmgr reports
# "No BootOrder is set; firmware will attempt recovery"), which crashed
# the original version of this script (it assumed BootOrder always exists).
#
# Current setup (as of 2026-08-08): boots via a NVRAM entry named
# "Ubuntu GRUB" pointing directly at grubx64.efi (shim removed -- fine
# since Secure Boot is disabled on this system, confirmed via mokutil).
# The UEFI-spec removable-media fallback path
# (/boot/efi/EFI/BOOT/BOOTX64.EFI) has proven to be the more reliable
# recovery mechanism on this firmware -- it's what actually got the system
# booting when BootOrder was wiped entirely. So: best-effort fix the NVRAM
# entry/order when possible, but ALWAYS keep the fallback file in sync
# regardless, since that's the real safety net.
set -uo pipefail

UBUNTU_BOOTNUM=$(efibootmgr | grep -oP '^Boot\K[0-9A-Fa-f]{4}(?=\* Ubuntu)' | head -1)

if [ -n "$UBUNTU_BOOTNUM" ]; then
  CURRENT_ORDER=$(efibootmgr | grep -oP '^BootOrder: \K.*' || true)
  FIRST=$(echo "$CURRENT_ORDER" | cut -d, -f1)
  if [ "$FIRST" != "$UBUNTU_BOOTNUM" ]; then
    REST=$(echo "$CURRENT_ORDER" | tr ',' '\n' | grep -v "^${UBUNTU_BOOTNUM}$" | paste -sd,)
    if [ -n "$REST" ]; then
      efibootmgr -o "${UBUNTU_BOOTNUM},${REST}"
    else
      efibootmgr -o "${UBUNTU_BOOTNUM}"
    fi
    echo "fix-efi-bootorder: reordered, Ubuntu (${UBUNTU_BOOTNUM}) now first"
  fi
else
  echo 'fix-efi-bootorder: no Ubuntu EFI boot entry found in efibootmgr, skipping reorder' >&2
fi

# Primary safety net regardless of the above: keep the UEFI-spec fallback
# path in sync with the known-working grubx64.efi. This is what actually
# got the system booting on 2026-08-08 when BootOrder itself was wiped.
if [ -f /boot/efi/EFI/ubuntu/grubx64.efi ]; then
  cp -f /boot/efi/EFI/ubuntu/grubx64.efi /boot/efi/EFI/BOOT/BOOTX64.EFI
fi
