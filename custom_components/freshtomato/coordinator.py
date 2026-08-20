"""Data coordinator for FreshTomato integration.

Design goal: MINIMUM HTTP calls per poll cycle.
─────────────────────────────────────────────────
Cycle (every scan_interval seconds):
  Call 1 – POST /update.cgi  exec=devlist
      → wldev, wlnoise, dhcpd_lease, arplist, wanip/gateway/netmask/uptime/lease

  Call 2 – POST /update.cgi  exec=netdev
      → netdev (per-interface byte counters)

  Call 3 – POST /update.cgi  exec=etherstates
      → physical Ethernet port link states

  Call 4 – GET /status-data.jsx
      → CPU/RAM sysinfo; reused for wlstats/NVRAM parsing on refresh cycles

Every NVRAM_REFRESH_INTERVAL cycles (~5 min):
  Call 5 – POST /update.cgi  exec=nvram
      → firmware, model, SSID, radio state, WAN proto, LAN IP, etc.

Total: 4 calls/cycle + one NVRAM call and occasional about.asp fallback.

Wireless client / repeater mode:
  When the router operates as a wireless client (no WAN IP, DHCP server
  disabled), WAN sensors return None and the device-list is sourced from
  the wldev table only (no dhcpd_lease). This is detected automatically.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    CannotConnect,
    FreshTomatoAPI,
    InvalidAuth,
    RouterData,
    WanConnection,
    calculate_cpu_usage,
)
from .const import (
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MWAN_MAX,
    NVRAM_REFRESH_INTERVAL,
    NVRAM_VARS,
)
from .ssh import check_ssh_available

_LOGGER = logging.getLogger(__name__)


class FreshTomatoCoordinator(DataUpdateCoordinator[RouterData]):
    """Coordinator: fetches all FreshTomato data and distributes to platforms."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: FreshTomatoAPI,
        entry: ConfigEntry,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
    ) -> None:
        self.api = api
        self._entry = entry
        self._cycle_count = 0
        self._nvram_cache: dict[str, str] = {}
        self._nvram_supported: bool | None = None
        # Detected router operating mode
        self.wireless_client_mode: bool = False
        # Keys written optimistically by switches/buttons → remaining guard cycles.
        # A key is skipped during nvram merges while its counter is > 0.
        # The counter is decremented once per full update cycle and removed when
        # it reaches 0.  Using 2 cycles gives the router time to persist the
        # change before we trust the polled value again.
        self._pending_nvram_writes: dict[str, int] = {}
        # SSH availability — checked once on first coordinator cycle.
        # None  = not yet checked
        # True  = SSH reachable (connection handshake succeeded)
        # False = SSH unreachable or authentication failed for all candidates
        self.ssh_available: bool | None = None
        # The SSH username that succeeded during the availability check.
        # Always a string (defaults to "root") so callers don't need to guard.
        self.ssh_username: str = "root"
        # Live hardware RF radio state per unit, from wlstats in status-data.jsx.
        # Updated every nvram refresh cycle (cycle 1 + every NVRAM_REFRESH_INTERVAL).
        # {unit_index: True=on, False=off}
        self._wl_hw_radio: dict[int, bool] = {}
        self._previous_cpu_jiffies: tuple[int, int] | None = None
        # Per-unit cooldown: monotonic timestamp after which the next Interface
        # toggle is allowed.  Set by the Interface switch after each successful
        # SSH command.  Radio switch does not use cooldown.
        # {unit_index: float (monotonic time)}
        self.iface_cooldown_until: dict[int, float] = {}

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} ({entry.data.get('host', 'router')})",
            update_interval=timedelta(seconds=scan_interval),
        )

    async def _async_update_data(self) -> RouterData:
        self._cycle_count += 1
        data = RouterData()

        try:
            devlist_raw = await self.api.fetch_devlist()
            _parse_devlist(devlist_raw, data)

            # The devlist response embeds a full nvram dict every cycle.
            # Merge it into the cache — but skip any keys that were written
            # optimistically (e.g. wl0_radio after a switch toggle) so a stale
            # router response cannot immediately undo the write.
            inline_nvram = devlist_raw.get("nvram", {})
            if isinstance(inline_nvram, dict):
                self._nvram_cache.update({
                    k: str(v)
                    for k, v in inline_nvram.items()
                    if v != "" and k not in self._pending_nvram_writes
                })
            if self._pending_nvram_writes:
                _LOGGER.debug(
                    "Skipped inline nvram overwrite for pending keys: %s",
                    list(self._pending_nvram_writes),
                )

            # Check SSH availability once on first cycle (cached for lifetime
            # of this coordinator instance).  Runs after devlist so we already
            # have host/port credentials available from entry.data.
            if self.ssh_available is None:
                await self._check_ssh()

            # Debug: log etherstates parse result so we can diagnose port issues
            _LOGGER.debug(
                "etherstates raw=%r  eth_ports=%r",
                devlist_raw.get("etherstates"),
                data.eth_ports,
            )

            netdev_raw = await self.api.fetch_netdev()
            _parse_netdev(netdev_raw, data)

            # Fetch physical port states — exec=etherstates is a separate call;
            # this router does not embed etherstates in the devlist response.
            etherstates = await self.api.fetch_etherstates()
            _parse_etherstates_dict(etherstates, data)

            # System telemetry is optional: a malformed/unavailable status page
            # must not discard the successfully fetched network data.
            status_text: str | None = None
            try:
                status_text = await self.api.fetch_status_data_text()
                data.system = self.api.parse_system_stats(status_text)
                current = None
                if (
                    data.system.cpu_total_jiffies is not None
                    and data.system.cpu_idle_jiffies is not None
                ):
                    current = (
                        data.system.cpu_total_jiffies,
                        data.system.cpu_idle_jiffies,
                    )
                data.system.cpu_usage_percent = calculate_cpu_usage(
                    self._previous_cpu_jiffies, current
                )
                if current is not None:
                    if (
                        self._previous_cpu_jiffies is not None
                        and data.system.cpu_usage_percent is None
                    ):
                        _LOGGER.debug(
                            "CPU jiffies reset or invalid delta; resetting baseline"
                        )
                    self._previous_cpu_jiffies = current
            except (CannotConnect, InvalidAuth, ValueError, TypeError) as err:
                _LOGGER.debug("Optional system telemetry unavailable: %s", err)

            if self._cycle_count == 1 or (self._cycle_count % NVRAM_REFRESH_INTERVAL == 0):
                await self._refresh_nvram(status_text)

            # Decrement the per-key guard counters; remove expired ones.
            # Guards are set to _PENDING_GUARD_CYCLES by switch entities and
            # count down to 0 over successive cycles so the optimistic cache
            # value is protected until the router has had time to persist it.
            expired = [k for k, n in self._pending_nvram_writes.items() if n <= 1]
            for k in expired:
                del self._pending_nvram_writes[k]
            for k in self._pending_nvram_writes:
                self._pending_nvram_writes[k] -= 1

        except InvalidAuth as err:
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except CannotConnect as err:
            raise UpdateFailed(f"Cannot connect to router: {err}") from err

        data.nvram = dict(self._nvram_cache)
        # Populate live hardware radio state from the last wlstats refresh.
        data.wl_hw_radio = dict(self._wl_hw_radio)

        # ── Build wan_connections list (supports 1–MWAN_MAX WANs) ──────────
        # WAN1 uses the legacy unprefixed keys (wan_ipaddr, wan_proto, …).
        # WAN2–WAN4 use wanN_* keys (wan2_ipaddr, wan2_proto, …).
        # A WAN slot is included only when its proto is not "disabled" / empty.
        data.wan_connections = _build_wan_connections(data.nvram)

        # Legacy single-WAN scalars — always reflect WAN1 for backward compat
        if data.wan_connections:
            w1 = data.wan_connections[0]
            data.wan_ip      = w1.ip
            data.wan_netmask = w1.netmask
            data.wan_gateway = w1.gateway
        else:
            data.wan_ip      = data.nvram.get("wan_ipaddr", "").strip()
            data.wan_netmask = data.nvram.get("wan_netmask", "").strip()
            data.wan_gateway = (
                data.nvram.get("wan_gateway_get") or data.nvram.get("wan_gateway", "")
            ).strip()

        # Detect wireless client / repeater / bridge mode
        wan_proto = data.nvram.get("wan_proto", "")
        self.wireless_client_mode = (
            wan_proto in ("disabled", "") or
            (not data.wan_ip or data.wan_ip in ("", "0.0.0.0"))
        )

        return data

    def _safe_nvram_update(self, result: dict[str, str]) -> None:
        """Merge result into the nvram cache, skipping any pending-write keys."""
        filtered = {k: v for k, v in result.items() if k not in self._pending_nvram_writes}
        skipped = set(result.keys()) - set(filtered.keys())
        if skipped:
            _LOGGER.debug("_refresh_nvram: skipped pending keys: %s", skipped)
        self._nvram_cache.update(filtered)

    async def _check_ssh(self) -> None:
        """Check SSH availability and cache the result in self.ssh_available.

        Tries 'root' first (FreshTomato default SSH user), then the configured
        HTTP username as fallback.  Stores the working username in
        self.ssh_username for use by all subsequent SSH commands.

        The SSH port defaults to 22; override via entry.data['ssh_port'].
        """
        host     = self._entry.data["host"]
        port     = int(self._entry.data.get("ssh_port", 22))
        username = self._entry.data.get("username", "admin")
        password = self._entry.data.get("password", "")
        self.ssh_available, self.ssh_username = await check_ssh_available(
            host, port, username, password
        )

    async def _refresh_nvram(self, status_text: str | None = None) -> None:
        if self._nvram_supported is not False:
            try:
                result = await self.api.fetch_nvram(NVRAM_VARS)
                if result:
                    self._safe_nvram_update(result)
                    self._nvram_supported = True
                    # exec=nvram doesn't provide wlstats — fall through to ASP
                    # for wlstats even when nvram is available.
            except Exception:  # pylint: disable=broad-except
                self._nvram_supported = False

        try:
            nvram_result, wl_hw_radio = await self.api.fetch_nvram_from_asp(
                NVRAM_VARS, status_text
            )
            if nvram_result:
                self._safe_nvram_update(nvram_result)
                if self._nvram_supported is None:
                    self._nvram_supported = False  # exec=nvram not used
            if wl_hw_radio:
                self._wl_hw_radio.update(wl_hw_radio)
        except (CannotConnect, InvalidAuth) as err:
            _LOGGER.warning("NVRAM ASP fallback failed: %s", err)

        # Firmware version is not exposed via exec=nvram on all builds.
        # Fall back to parsing it from the about.asp / status-overview.asp HTML.
        if not any(
            k in self._nvram_cache
            for k in ("t_build_time", "os_version", "tomato_version")
        ):
            fw = await self.api.fetch_about_page()
            _LOGGER.debug("fetch_about_page returned: %r", fw)
            if fw:
                self._nvram_cache["t_build_time"] = fw
        _LOGGER.debug(
            "nvram firmware keys: t_build_time=%r os_version=%r tomato_version=%r",
            self._nvram_cache.get("t_build_time"),
            self._nvram_cache.get("os_version"),
            self._nvram_cache.get("tomato_version"),
        )


# ── Parsing helpers ───────────────────────────────────────────────────────────

def _parse_devlist(raw: dict[str, Any], data: RouterData) -> None:
    """Parse all fields from exec=devlist response.

    Actual FreshTomato devlist response keys (verified from live logs):
      wldev       – flat list of client entries OR nested per-radio list
                    flat entry:   [iface, mac, rssi, tx_rate, rx_rate, uptime, assoc]
                    nested entry: [[mac, rssi, tx_rate, rx_rate, iface, uptime, assoc], ...]
      wlnoise     – [noise_radio0, noise_radio1] (dBm integers)
      dhcpd_lease – [[name, mac, ip, ttl_sec], ...]
      arplist     – [[ip, mac, iface, name], ...]
      nvram       – dict of nvram key/value pairs (includes WAN data)
      etherstates – dict of port states (already parsed by JS parser)
      gc_time     – int (ignored)
    """

    # Wireless clients — handle both flat and nested-per-radio formats
    # Flat:   [iface, mac, rssi, tx, rx, uptime, assoc]  (iface is a string like 'eth1')
    # Nested: [[mac, rssi, tx, rx, iface, uptime, assoc], ...] per radio
    wldev_raw = raw.get("wldev", [])
    if isinstance(wldev_raw, list):
        for entry in wldev_raw:
            if not isinstance(entry, list):
                continue
            # Detect format: flat if first element is a string interface name
            if entry and isinstance(entry[0], str) and not _looks_like_mac(str(entry[0])):
                # Flat format: [iface, mac, rssi, tx_rate, rx_rate, uptime, assoc]
                if len(entry) < 2:
                    continue
                mac = str(entry[1]).upper().strip()
                if not mac or mac == "00:00:00:00:00:00":
                    continue
                data.wireless_clients.append({
                    "mac": mac,
                    "rssi": _safe_int(entry[2]) if len(entry) > 2 else None,
                    "tx_rate": _safe_int(entry[3]) if len(entry) > 3 else None,
                    "rx_rate": _safe_int(entry[4]) if len(entry) > 4 else None,
                    "iface": str(entry[0]).strip(),
                })
            elif entry and isinstance(entry[0], list):
                # Nested per-radio format: [[client, ...], ...]
                for client in entry:
                    if not isinstance(client, list) or len(client) < 1:
                        continue
                    mac = str(client[0]).upper().strip()
                    if not mac or mac == "00:00:00:00:00:00":
                        continue
                    data.wireless_clients.append({
                        "mac": mac,
                        "rssi": _safe_int(client[1]) if len(client) > 1 else None,
                        "tx_rate": _safe_int(client[2]) if len(client) > 2 else None,
                        "rx_rate": _safe_int(client[3]) if len(client) > 3 else None,
                        "iface": str(client[4]).strip() if len(client) > 4 else "",
                    })
            else:
                # Single flat client entry (mac first)
                mac = str(entry[0]).upper().strip()
                if not mac or mac == "00:00:00:00:00:00":
                    continue
                data.wireless_clients.append({
                    "mac": mac,
                    "rssi": _safe_int(entry[1]) if len(entry) > 1 else None,
                    "tx_rate": _safe_int(entry[2]) if len(entry) > 2 else None,
                    "rx_rate": _safe_int(entry[3]) if len(entry) > 3 else None,
                    "iface": str(entry[4]).strip() if len(entry) > 4 else "",
                })

    # Noise floors
    wlnoise_raw = raw.get("wlnoise", [])
    if isinstance(wlnoise_raw, list):
        data.wl_noise = [_safe_int(n) for n in wlnoise_raw]

    # DHCP leases
    for entry in raw.get("dhcpd_lease", []) or []:
        if not isinstance(entry, list) or len(entry) < 3:
            continue
        data.dhcp_leases.append({
            "name": str(entry[0]).strip(),
            "mac": str(entry[1]).upper().strip(),
            "ip": str(entry[2]).strip(),
            "lease": _safe_int(entry[3]) if len(entry) > 3 else 0,
        })

    # ARP table
    for entry in (raw.get("arplist") or raw.get("arp") or []):
        if not isinstance(entry, list) or len(entry) < 2:
            continue
        data.arp_table.append({
            "ip": str(entry[0]).strip(),
            "mac": str(entry[1]).upper().strip(),
            "iface": str(entry[2]).strip() if len(entry) > 2 else "",
            "name": str(entry[3]).strip() if len(entry) > 3 else "",
        })

    # WAN data comes from inline nvram merged into _nvram_cache in _async_update_data.
    # data.wan_ip/gateway are set below after data.nvram is assigned.
    # (eth_ports is populated separately via fetch_etherstates in _async_update_data)


def _parse_etherstates_dict(es: dict[str, str], data: RouterData) -> None:
    """Populate data.eth_ports from the clean {portN: state} dict returned
    by api.fetch_etherstates().

    Port → label mapping (standard Broadcom, EA6700 confirmed):
        port0 = WAN
        port1 = LAN 0  (FreshTomato UI uses 0-indexed LAN labels)
        port2 = LAN 1
        port3 = LAN 2
        port4 = LAN 3
    Ports absent on this hardware are returned as "disabled" and skipped.
    """
    _PORT_LABELS: dict[str, str] = {
        "port0": "WAN",
        "port1": "LAN0",
        "port2": "LAN1",
        "port3": "LAN2",
        "port4": "LAN3",
    }
    for port_key, state in es.items():
        if state in ("disabled", ""):
            continue
        label = _PORT_LABELS.get(port_key, port_key.replace("port", "Port "))
        data.eth_ports[label] = state


def _parse_netdev(raw: dict[str, Any], data: RouterData) -> None:
    """Parse netdev byte counters.

    FreshTomato netdev response:
        netdev = {'eth0':{'rx':123,'tx':456,'rxp':12,'txp':34}, ...}
    Keys are interface names; values are dicts with rx/tx byte counters.
    """
    netdev_raw = raw.get("netdev", {})
    if not isinstance(netdev_raw, dict):
        return
    for iface, counters in netdev_raw.items():
        if not isinstance(counters, dict):
            continue
        data.netdev[str(iface)] = {
            "rx": _safe_int(counters.get("rx", 0)),
            "tx": _safe_int(counters.get("tx", 0)),
            "rxp": _safe_int(counters.get("rxp", 0)),
            "txp": _safe_int(counters.get("txp", 0)),
        }



def _build_wan_connections(nvram: dict[str, str]) -> list[WanConnection]:
    """Build a list of WanConnection objects from the nvram cache.

    FreshTomato naming convention:
      WAN1  → wan_ipaddr / wan_proto / wan_gateway / wan_netmask / wan_ifname …
      WAN2  → wan2_ipaddr / wan2_proto / … (note: NO underscore between "wan" and the digit)
      WAN3  → wan3_ipaddr / …
      WAN4  → wan4_ipaddr / …

    A WAN slot is included in the result only when its proto is not empty or
    "disabled".  This mirrors what the FreshTomato GUI shows as "active" WANs.
    """
    connections: list[WanConnection] = []
    for idx in range(1, MWAN_MAX + 1):
        if idx == 1:
            prefix = "wan"   # legacy: wan_ipaddr, wan_proto, etc.
        else:
            prefix = f"wan{idx}"  # wan2_ipaddr, wan3_proto, etc.

        proto = nvram.get(f"{prefix}_proto", "").strip()
        if proto in ("", "disabled"):
            # If this is WAN1 and proto is missing/disabled we still include
            # it so legacy sensors don't vanish; for WAN2+ we skip silently.
            if idx > 1:
                continue

        ip      = nvram.get(f"{prefix}_ipaddr", "").strip()
        netmask = nvram.get(f"{prefix}_netmask", "").strip()
        gateway = (
            nvram.get(f"{prefix}_gateway_get") or nvram.get(f"{prefix}_gateway", "")
        ).strip()
        ifname  = (
            nvram.get(f"{prefix}_ifname") or nvram.get(f"{prefix}_ifnames", "")
        ).strip().split()[0] if (
            nvram.get(f"{prefix}_ifname") or nvram.get(f"{prefix}_ifnames", "")
        ).strip() else ""
        dns     = (
            nvram.get(f"{prefix}_get_dns") or nvram.get(f"{prefix}_dns", "")
        ).strip()

        connections.append(WanConnection(
            index   = idx,
            ip      = ip,
            netmask = netmask,
            gateway = gateway,
            proto   = proto,
            ifname  = ifname,
            dns     = dns,
        ))

    return connections


def _looks_like_mac(s: str) -> bool:
    """Return True if s looks like a MAC address (AA:BB:CC:DD:EE:FF)."""
    return bool(re.match(r'^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$', s))


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
