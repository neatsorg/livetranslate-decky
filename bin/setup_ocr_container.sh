#!/usr/bin/env bash
# Provisions the distrobox container ocr_worker.py always runs inside (see
# main.py's _distrobox_python_command) - required for every OCR engine,
# including the default Chrome Screen AI, not just the Tesseract fallback.
# Idempotent: safe to re-run. Invoked by main.py's provision_ocr_container()
# and logged to data_dir/ocr-setup.log; also runnable by hand for debugging.
set -euo pipefail

CONTAINER_NAME="${PLAYTRANSLATE_OCR_BOX:-playtranslate-ocr}"
CONTAINER_IMAGE="${PLAYTRANSLATE_OCR_IMAGE:-ubuntu:24.04}"
LOCAL_BIN="$HOME/.local/bin"
export PATH="$LOCAL_BIN:$PATH"

# Pinned rather than resolved against GitHub's "latest" release API, so this
# step doesn't depend on that API being reachable/unrate-limited at setup
# time. Bump both together when a newer podman-launcher is needed.
PODMAN_LAUNCHER_VERSION="v0.0.5"
PODMAN_LAUNCHER_SHA256="689c841cb5e9f86dec84b095b7cc2581b2af51e7ee3bb3e8067b92487b312b1d"
PODMAN_LAUNCHER_URL="https://github.com/89luca89/podman-launcher/releases/download/${PODMAN_LAUNCHER_VERSION}/podman-launcher-amd64"

log() {
  echo "[setup_ocr_container] $*"
}

install_distrobox() {
  if command -v distrobox >/dev/null 2>&1; then
    log "distrobox already on PATH: $(command -v distrobox)"
    return
  fi
  log "STEP: installing distrobox to ${LOCAL_BIN}"
  curl -fsSL https://raw.githubusercontent.com/89luca89/distrobox/main/install \
    | sh -s -- --prefix "$HOME/.local"
}

install_podman() {
  if command -v podman >/dev/null 2>&1; then
    log "podman already on PATH: $(command -v podman)"
  else
    # distrobox's own extras/install-podman helper is deprecated upstream -
    # confirmed live: it now just prints a deprecation notice pointing at
    # podman-launcher and exits 1, installing nothing. podman-launcher is a
    # single static rootless-podman binary; sha256-checked against the
    # pinned version above.
    log "STEP: installing podman-launcher ${PODMAN_LAUNCHER_VERSION} to ${LOCAL_BIN}"
    mkdir -p "$LOCAL_BIN"
    curl -fsSL -o "${LOCAL_BIN}/podman" "$PODMAN_LAUNCHER_URL"
    echo "${PODMAN_LAUNCHER_SHA256}  ${LOCAL_BIN}/podman" | sha256sum -c -
    chmod +x "${LOCAL_BIN}/podman"
  fi

  # Rootless podman needs a signature policy to pull images - confirmed
  # live: without one, `distrobox create` fails with "open
  # /etc/containers/policy.json: no such file or directory" (podman-launcher
  # doesn't ship a default, and /etc is root-owned). The per-user path is
  # checked first by podman and needs no sudo.
  if [[ ! -f "$HOME/.config/containers/policy.json" ]]; then
    log "STEP: writing default container signature policy"
    mkdir -p "$HOME/.config/containers"
    cat > "$HOME/.config/containers/policy.json" <<'JSON'
{
    "default": [
        {
            "type": "insecureAcceptAnything"
        }
    ]
}
JSON
  fi
}

container_exists() {
  distrobox list --no-color 2>/dev/null \
    | awk -F'|' 'NR>1 { gsub(/^[ \t]+|[ \t]+$/, "", $2); print $2 }' \
    | grep -qx "$CONTAINER_NAME"
}

create_container() {
  log "STEP: creating distrobox container '${CONTAINER_NAME}' (image: ${CONTAINER_IMAGE})"
  distrobox create --name "$CONTAINER_NAME" --image "$CONTAINER_IMAGE" --yes --no-entry
}

install_python_deps() {
  # Confirmed live on Ubuntu 24.04: python3 ships without pip, and Debian/
  # Ubuntu's PEP 668 "externally managed environment" guard blocks a plain
  # `pip install --user` even once pip exists - --break-system-packages is
  # safe here since this container is dedicated to this plugin's OCR worker,
  # not a shared system Python.
  log "STEP: ensuring pip is available inside '${CONTAINER_NAME}'"
  distrobox enter "$CONTAINER_NAME" -- bash -c '
    set -e
    if ! python3 -m pip --version >/dev/null 2>&1; then
      sudo apt-get update -qq
      sudo apt-get install -y -qq --no-install-recommends python3-pip
    fi
  '

  log "STEP: installing Python deps (Pillow, protobuf) inside '${CONTAINER_NAME}'"
  distrobox enter "$CONTAINER_NAME" -- bash -c '
    set -e
    python3 -m pip install --user --break-system-packages --upgrade pip
    python3 -m pip install --user --break-system-packages Pillow "protobuf>=7.35.1"
  '
}

main() {
  install_distrobox
  install_podman

  if container_exists; then
    log "container '${CONTAINER_NAME}' already exists, skipping creation"
  else
    create_container
  fi

  install_python_deps
  log "DONE"
}

main "$@"
