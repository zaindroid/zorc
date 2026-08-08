#!/bin/bash
# HP ZBook firmware quirk workaround: the UEFI NVRAM 'Ubuntu' boot entry
# periodically gets dropped from BootOrder (though the entry itself stays
# defined), leaving only legacy BBS-style entries that don't reliably chain
# into the real bootloader. This reasserts the correct order on every boot
# and keeps the UEFI-spec fallback path in sync as a second line of defense.
set -euo pipefail

UBUNTU_BOOTNUM=$(efibootmgr | grep -oP '^Boot\K[0-9A-Fa-f]{4}(?=\* Ubuntu)')
if [ -z "$UBUNTU_BOOTNUM" ]; then
  echo 'fix-efi-bootorder: no Ubuntu EFI boot entry found, nothing to do' >&2
  exit 0
fi

CURRENT_ORDER=$(efibootmgr | grep -oP '^BootOrder: \K.*')
FIRST=$(echo "$CURRENT_ORDER" | cut -d, -f1)
if [ "$FIRST" != "$UBUNTU_BOOTNUM" ]; then
  REST=$(echo "$CURRENT_ORDER" | tr ',' '\n' | grep -v "^${UBUNTU_BOOTNUM}$" | paste -sd,)
  efibootmgr -o "${UBUNTU_BOOTNUM},${REST}"
  echo "fix-efi-bootorder: reordered, Ubuntu (${UBUNTU_BOOTNUM}) now first"
fi

if [ -f /boot/efi/EFI/ubuntu/shimx64.efi ]; then
  cp -f /boot/efi/EFI/ubuntu/shimx64.efi /boot/efi/EFI/BOOT/BOOTX64.EFI
fi
