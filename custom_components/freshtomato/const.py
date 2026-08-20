"""Constants for the FreshTomato integration."""
from __future__ import annotations

DOMAIN = "freshtomato"

# Config entry keys
CONF_HTTP_ID = "http_id"
CONF_TRACK_WIRED = "track_wired"

# Defaults
DEFAULT_PORT = 80
DEFAULT_SSL = False
DEFAULT_VERIFY_SSL = True
DEFAULT_SCAN_INTERVAL = 30
DEFAULT_TRACK_WIRED = True
DEFAULT_USERNAME = "admin"

# Coordinator update keys
DATA_COORDINATOR = "coordinator"

# update.cgi exec targets are kept as separate calls for compatibility:
#   1. exec=devlist  → wlnoise[], wldev[], dhcpd_lease[], arp[], active WAN stats
#   2. exec=netdev   → real-time interface byte counters (tx/rx)
#   3. exec=etherstates → physical port states
# Every cycle also GETs status-data.jsx for CPU/RAM. A targeted exec=nvram POST
# is added only on periodic refresh cycles; that cycle reuses the status page.

EXEC_DEVLIST = "devlist"
EXEC_NETDEV = "netdev"

# NVRAM variables to fetch in one call from update.cgi?exec=nvram
# These are mostly static (router name, firmware, WAN config) and are fetched
# once on startup plus every STATUS_NVRAM_INTERVAL cycles.
NVRAM_VARS = [
    "t_model_name",
    "t_build_time",
    "os_version",
    "wan_ipaddr",
    "wan_netmask",
    "wan_gateway",
    "wan_proto",
    "wan_dns",
    "wan_ifname",
    "wan_ifnames",
    "wan_lease",
    "wan_get_dns",
    "ppp_get_ip",
    "lan_ipaddr",
    "lan1_ipaddr",
    "lan_netmask",
    "lan_hostname",
    "lan_gateway",
    "wl0_ifname",
    "wl0_ssid",
    "wl0_channel",
    "wl0_radio",
    "wl0_mode",
    "wl0_net_mode",
    "wl0_security_mode",
    "wl0_closed",
    "wl1_ssid",
    "wl1_channel",
    "wl1_radio",
    "wl1_ifname",
    "wl1_mode",
    "wl1_net_mode",
    "wl1_security_mode",
    "wl1_closed",
    # Band steering (requires both radios active in AP mode)
    # FreshTomato Band Steering Daemon (BSD) enable key
    "bsd_enabled",
    "http_id",
    "uptime",
    "cpu_temp",
    "t_cpu_temp",
    # MultiWAN: number of active WAN ports configured in FreshTomato GUI
    "mwan_num",
    # WAN2 scalars (primary WAN uses wan_* without a number)
    "wan2_ipaddr",
    "wan2_netmask",
    "wan2_gateway",
    "wan2_gateway_get",
    "wan2_proto",
    "wan2_ifname",
    "wan2_ifnames",
    "wan2_dns",
    "wan2_get_dns",
    "wan2_lease",
    # WAN3 scalars
    "wan3_ipaddr",
    "wan3_netmask",
    "wan3_gateway",
    "wan3_gateway_get",
    "wan3_proto",
    "wan3_ifname",
    "wan3_ifnames",
    "wan3_dns",
    "wan3_get_dns",
    "wan3_lease",
    # WAN4 scalars
    "wan4_ipaddr",
    "wan4_netmask",
    "wan4_gateway",
    "wan4_gateway_get",
    "wan4_proto",
    "wan4_ifname",
    "wan4_ifnames",
    "wan4_dns",
    "wan4_get_dns",
    "wan4_lease",
]

# How many poll cycles between full NVRAM refreshes (semi-static data)
NVRAM_REFRESH_INTERVAL = 10

# Maximum number of WAN interfaces FreshTomato supports (hardware limit).
# WAN1 uses legacy keys (wan_ipaddr, …); WAN2-4 use wanN_* keys.
MWAN_MAX = 4

# Platform constants
PLATFORMS = ["sensor", "binary_sensor", "button", "switch", "device_tracker"]
