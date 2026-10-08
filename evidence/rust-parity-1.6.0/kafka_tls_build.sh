#!/bin/sh
# Run in the dedicated rust:1.74-bookworm test container with repository mounted
# read-only at /source. Build products and package installations stay in it.
set -eu
apt-get update
apt-get install -y --no-install-recommends cmake pkg-config libcurl4-openssl-dev perl
mkdir -p /work
tar -C /source/crates/pollardai --exclude=target -cf - . | tar -C /work -xf -
cd /work
cargo test --locked --features kafka-tls --lib kafka::tests
