#!/usr/bin/env python3
"""
Minimal, dependency-free generator for Cloudflare WARP WireGuard profiles.

Inspired by and based on the functionality of the `wgcf` project
(https://github.com/ViRb3/wgcf/), this script reimplements the essential steps
in pure Python: generating a Curve25519 keypair, registering a WARP device via
Cloudflare's client API, and producing a ready-to-use WireGuard config. Designed
to remain small, transparent, and easily auditable.
"""

import os
import sys
import json
import base64
import secrets
import urllib.request
from datetime import datetime, timezone
from typing import Any

API_BASE = "https://api.cloudflareclient.com/v0a1922"
BASE_HEADERS = {
    "User-Agent": "okhttp/3.12.1",
    "CF-Client-Version": "a-6.3-1922",
    "Content-Type": "application/json",
}


def clamp(private_key_bytes: bytes) -> bytes:
    """Clamp the private key according to Curve25519 specification."""
    clamped_key = bytearray(private_key_bytes)
    clamped_key[0] &= 248
    clamped_key[31] &= 127
    clamped_key[31] |= 64
    return bytes(clamped_key)


def x25519(private_key: bytes, public_key_u_coordinate: bytes) -> bytes:
    """Perform X25519 elliptic curve Diffie-Hellman operation."""
    curve_prime = 2**255 - 19
    montgomery_coeff_a24 = 121665

    scalar_k = int.from_bytes(clamp(private_key), "little")
    u_coordinate = int.from_bytes(public_key_u_coordinate, "little")

    x_1, x_2, z_2, x_3, z_3 = u_coordinate, 1, 0, u_coordinate, 1
    should_swap = 0

    for bit_index in range(254, -1, -1):
        current_bit = (scalar_k >> bit_index) & 1
        should_swap ^= current_bit

        x_2, x_3 = (x_3, x_2) if should_swap else (x_2, x_3)
        z_2, z_3 = (z_3, z_2) if should_swap else (z_2, z_3)
        should_swap = current_bit

        sum_x2_z2 = (x_2 + z_2) % curve_prime
        sum_x2_z2_squared = (sum_x2_z2 * sum_x2_z2) % curve_prime
        diff_x2_z2 = (x_2 - z_2) % curve_prime
        diff_x2_z2_squared = (diff_x2_z2 * diff_x2_z2) % curve_prime
        e_term = (sum_x2_z2_squared - diff_x2_z2_squared) % curve_prime
        sum_x3_z3 = (x_3 + z_3) % curve_prime
        diff_x3_z3 = (x_3 - z_3) % curve_prime
        cross_term_da = (diff_x3_z3 * sum_x2_z2) % curve_prime
        cross_term_cb = (sum_x3_z3 * diff_x2_z2) % curve_prime

        x_3 = pow((cross_term_da + cross_term_cb) % curve_prime, 2, curve_prime)
        z_3 = (
            x_1 * pow((cross_term_da - cross_term_cb) % curve_prime, 2, curve_prime)
        ) % curve_prime
        x_2 = (sum_x2_z2_squared * diff_x2_z2_squared) % curve_prime
        z_2 = (
            e_term * (sum_x2_z2_squared + montgomery_coeff_a24 * e_term)
        ) % curve_prime

    x_2, x_3 = (x_3, x_2) if should_swap else (x_2, x_3)
    z_2, z_3 = (z_3, z_2) if should_swap else (z_2, z_3)

    result_x_coordinate = x_2 * pow(z_2, curve_prime - 2, curve_prime) % curve_prime
    return result_x_coordinate.to_bytes(32, "little")


def generate_keypair():
    """Generate a Curve25519 keypair and return it as base64-encoded strings."""
    private = secrets.token_bytes(32)
    public = x25519(private, bytes.fromhex("09" + "00" * 31))

    return (base64.b64encode(private).decode(), base64.b64encode(public).decode())


def api_request(
    endpoint: str, data: dict[str, Any] | None = None, token: str | None = None
) -> dict[str, Any]:
    """Make an API request to the Cloudflare WARP client API."""
    headers = BASE_HEADERS.copy()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(
        f"{API_BASE}/{endpoint}",
        data=json.dumps(data).encode() if data else None,
        headers=headers,
    )

    with urllib.request.urlopen(req) as response:
        return dict(json.loads(response.read().decode()))


def create_profile(output_path: str = "wgcf.conf") -> None:
    """Create a WireGuard profile and save it to the specified output path."""
    private_key, public_key = generate_keypair()

    result: dict[str, Any] = api_request(
        "reg",
        {
            "key": public_key,
            "install_id": "",
            "fcm_token": "",
            "tos": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "type": "Android",
            "model": "PC",
            "locale": "en_US",
        },
    )

    device_id = result["id"]

    config: dict[str, Any] = api_request(f"reg/{device_id}", token=result["token"])
    iface: dict[str, Any] = config["config"]["interface"]
    peer: dict[str, Any] = config["config"]["peers"][0]

    profile = f"""[Interface]
PrivateKey = {private_key}
Address = {iface['addresses']['v4']}/32
DNS = 1.1.1.1
MTU = 1280

[Peer]
PublicKey = {peer['public_key']}
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = {peer['endpoint']['host']}
PersistentKeepalive = 25

[Socks5]
BindAddress = 127.0.0.1:1080
"""

    with open(output_path, "w") as f:
        _ = f.write(profile)

    print(f"Profile created: {output_path}")
    print(f"Device ID: {device_id}")
    print(f"License: {result['account']['license']}")


def main() -> None:
    """Main entry point for the script."""
    output = sys.argv[1] if len(sys.argv) > 1 else "wgcf.conf"

    if os.path.exists(output):
        print(f"Error: Profile '{output}' already exists.")
        sys.exit(1)

    create_profile(output)


if __name__ == "__main__":
    main()
