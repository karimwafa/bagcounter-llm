#!/bin/bash
# =============================================================================
#  NVIDIA Driver & CUDA Toolkits Installer for Ubuntu 24.04/22.04
#  Digunakan untuk mengaktifkan GPU RTX 3050 agar bisa training AI
# =============================================================================

set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${YELLOW}[INFO]${NC}  $1"; }
success() { echo -e "${GREEN}[OK]${NC}    $1"; }
error()   { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

[[ $EUID -ne 0 ]] && error "Jalankan sebagai root!"

info "Menghapus driver NVIDIA lama (jika ada)..."
apt-get purge -y "nvidia*" "libnvidia*" -qq

info "Menambahkan repository NVIDIA..."
add-apt-repository ppa:graphics-drivers/ppa -y -q
apt-get update -qq

info "Mendeteksi driver yang direkomendasikan..."
RECOMMENDED=$(ubuntu-drivers devices | grep 'recommended' | awk '{print $3}')

if [ -z "$RECOMMENDED" ]; then
    info "Menginstall driver default nvidia-driver-535..."
    DRIVER="nvidia-driver-535"
else
    info "Menginstall driver rekomendasi: $RECOMMENDED..."
    DRIVER=$RECOMMENDED
fi

apt-get install -y "$DRIVER" nvidia-utils-535 -qq

info "Install CUDA Toolkit 12.1..."
apt-get install -y nvidia-cuda-toolkit -qq

success "Instalasi selesai!"
echo ""
echo -e "${YELLOW}PENTING: Server harus di-REBOOT agar driver aktif.${NC}"
echo -e "Setelah reboot, ketik perintah: ${GREEN}nvidia-smi${NC}"
echo ""
