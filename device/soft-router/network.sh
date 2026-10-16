#!/system/bin/sh
# No NAT, addrtype, OUTPUT interception, sysctl or Android table restore.
set -eu
ROOT=/data/adb/f50-soft-router
cd "$ROOT"
[ "$(cat owner)" = f50-soft-router-v1 ]
MARK=0x80000000/0x80000000

unhook() {
    for xt in iptables ip6tables; do
        while "$xt" -w 5 -t mangle -C PREROUTING -i br0 -j F50SR_TP 2>/dev/null; do
            "$xt" -w 5 -t mangle -D PREROUTING -i br0 -j F50SR_TP
        done
        if "$xt" -w 5 -C INPUT -i br0 -m mark --mark "$MARK" -j ACCEPT 2>/dev/null; then
            "$xt" -w 5 -D INPUT -i br0 -m mark --mark "$MARK" -j ACCEPT
        fi
    done
}
cleanup() {
    [ -f network-owned ] || return 0
    unhook
    for family in 4 6; do
        if ip -"$family" rule show | grep '^1789:' | grep -q 'lookup 17891'; then
            ip -"$family" rule del pref 1789 iif br0 fwmark "$MARK" lookup 17891
        fi
        if ip -"$family" route show table 17891 2>/dev/null | grep -q 'local default dev lo'; then
            ip -"$family" route del local default dev lo table 17891
        fi
    done
    for xt in iptables ip6tables; do
        if "$xt" -w 5 -t mangle -S F50SR_TP >/dev/null 2>&1; then
            "$xt" -w 5 -t mangle -F F50SR_TP
            "$xt" -w 5 -t mangle -X F50SR_TP
        fi
    done
    rm -f network-owned
}
case "${1:-}" in
prepare)
    cleanup
    for family in 4 6; do
        if ip -"$family" rule show | grep -q '^1789:'; then exit 40; fi
        [ -z "$(ip -"$family" route show table 17891 2>/dev/null)" ]
    done
    for xt in iptables ip6tables; do
        if "$xt" -w 5 -t mangle -S F50SR_TP >/dev/null 2>&1; then exit 40; fi
        if "$xt" -w 5 -C INPUT -i br0 -m mark --mark "$MARK" -j ACCEPT 2>/dev/null; then exit 40; fi
    done
    touch network-owned
    for family in 4 6; do
        xt=iptables; loopback=127.0.0.1
        [ "$family" = 4 ] || { xt=ip6tables; loopback=::1; }
        "$xt" -w 5 -t mangle -N F50SR_TP
        # DNS precedes private/local bypass: clients normally query the router.
        for proto in tcp udp; do
            "$xt" -w 5 -t mangle -A F50SR_TP -p "$proto" --dport 53 -j TPROXY --on-ip "$loopback" --on-port 17891 --tproxy-mark "$MARK"
        done
        # Snapshot local addresses and LAN prefixes without requiring xt_addrtype.
        ip -"$family" addr show | awk '/inet6? / {split($2,a,"/"); print a[1]}' > local-addresses
        while read -r address; do
            "$xt" -w 5 -t mangle -A F50SR_TP -d "$address" -j RETURN
        done < local-addresses
        ip -"$family" addr show dev br0 | awk '/inet6? / {print $2}' > lan-prefixes
        while read -r cidr; do
            "$xt" -w 5 -t mangle -A F50SR_TP -d "$cidr" -j RETURN
        done < lan-prefixes
        if [ "$family" = 4 ]; then
            ranges='0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.168.0.0/16 224.0.0.0/4 240.0.0.0/4'
        else
            ranges='::/128 ::1/128 fc00::/7 fe80::/10 ff00::/8'
        fi
        for cidr in $ranges; do
            "$xt" -w 5 -t mangle -A F50SR_TP -d "$cidr" -j RETURN
        done
        for proto in tcp udp; do
            "$xt" -w 5 -t mangle -A F50SR_TP -p "$proto" -j TPROXY --on-ip "$loopback" --on-port 17891 --tproxy-mark "$MARK"
        done
    done
    ;;
enable)
    [ -f network-owned ]
    for family in 4 6; do
        ip -"$family" route add local default dev lo table 17891
        ip -"$family" rule add pref 1789 iif br0 fwmark "$MARK" lookup 17891
    done
    for xt in iptables ip6tables; do
        "$xt" -w 5 -I INPUT 1 -i br0 -m mark --mark "$MARK" -j ACCEPT
        "$xt" -w 5 -t mangle -I PREROUTING 1 -i br0 -j F50SR_TP
    done
    ;;
check)
    [ -f network-owned ]
    for family in 4 6; do
        ip -"$family" rule show | grep '^1789:' | grep 'iif br0' | grep 'fwmark 0x80000000/0x80000000' | grep -q 'lookup 17891'
        ip -"$family" route show table 17891 | grep -q 'local default dev lo'
    done
    for xt in iptables ip6tables; do
        "$xt" -w 5 -C INPUT -i br0 -m mark --mark "$MARK" -j ACCEPT
        "$xt" -w 5 -t mangle -C PREROUTING -i br0 -j F50SR_TP
    done
    ;;
unhook) [ ! -f network-owned ] || unhook ;;
cleanup) cleanup ;;
*) exit 2;;
esac
