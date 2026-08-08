#!/bin/bash
# NOT YET WORKING — DO NOT RUN AS-IS. See docs/runbook.md / commit history for details.
#
# GPU is a Quadro K2100M (Kepler/GK106, PCI 10de:11fc). NVIDIA dropped Kepler
# support after the 470 driver branch. `ubuntu-drivers devices` recommends
# nothing for this card because none of the modern branches in the repo
# support it.
#
# Tried: `apt install nvidia-driver-470`. In Ubuntu 24.04's noble-updates repo
# this metapackage is no longer standalone — `apt-cache depends nvidia-driver-470`
# shows it now just Depends: nvidia-driver-535, which itself chains up to 580.
# Only the 580 DKMS module actually got built (dkms status showed
# nvidia/580.173.02, not 470) — 580 does not support Kepler, so this would
# have produced a dead GPU post-reboot. Confirmed and purged BEFORE rebooting,
# no downtime occurred; system is on nouveau, working state.
#
# Also tried installing the individual -470 component packages directly
# (nvidia-dkms-470, libnvidia-gl-470, nvidia-utils-470,
# xserver-xorg-video-nvidia-470) to sidestep the broken metapackage — apt
# still force-installs matching -535 siblings alongside them (dependency
# test via --dry-run), so this doesn't cleanly isolate a working 470-only
# install either.
#
# Next step under consideration: NVIDIA's official .run installer for
# 470.256.02 direct from nvidia.com, bypassing Ubuntu's package graph
# entirely — needs nouveau blacklisted + reboot into a non-X runlevel.
# Paused pending a decision on whether this GPU (2GB VRAM, ~10yr old mobile
# Kepler chip) is worth the effort versus running headless/CPU-only.
#
# RESOLUTION (2026-08-08): installed via NVIDIA's official .run installer
# instead of apt. See:
#   - bootstrap/blacklist-nouveau.conf equivalent: /etc/modprobe.d/blacklist-nouveau.conf
#   - installer command: sudo sh NVIDIA-Linux-x86_64-470.256.02.run --dkms --silent --no-x-check
#     (downloaded from https://download.nvidia.com/XFree86/Linux-x86_64/470.256.02/)
#   - required `apt install dkms` first (was auto-removed by an earlier autoremove
#     pass after the failed apt attempt was purged)
# Verified working live via nvidia-smi (470.256.02, CUDA 11.4, Quadro K2100M).
# DKMS-registered so it rebuilds automatically on future kernel updates.
#
# Also uncovered along the way: an HP ZBook 15 G2 firmware bug where the EFI
# boot order drops the real Ubuntu entry. Fixed separately — see
# bootstrap/05-fix-efi-bootorder.sh and docs/runbook.md.
