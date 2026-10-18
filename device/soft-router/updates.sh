# Internal functions; service.sh holds lifecycle.lock before calling these.
BB=/data/adb/magisk/busybox
BOOT=/data/adb/service.d/f50-soft-router.sh
CRON_LINE="0 */2 * * * $BB ash $ROOT/service.sh update"
private_root() {
    [ ! -L "$ROOT" ] && [ "$(stat -c %u:%a "$ROOT")" = 0:700 ]
}
sha() { sha256sum "$1" | awk '{print $1}'; }
identities() {
    owned mihomo && owned tailscaled || return 1
    cat mihomo.pid tailscaled.pid
}
same_processes() { [ "$(identities)" = "$identity" ]; }
api() {
    curl --silent --fail --noproxy '*' --max-time 10 --max-filesize 8388608 \
        --unix-socket "$ROOT/controller.sock" "http://localhost/$1"
}
# shortcut: the center promises indent=2 JSON; format drift is rejected, not converted.
envelope() {
    awk '
      /^  "/ {
        section=$0; sub(/^  "/,"",section); sub(/".*/,"",section)
        if (seen[section]++) exit 1
        if (section ~ /^(port|socks-port|redir-port|tproxy-port|tunnels|ss-config|vmess-config|tuic-server|secret)$/ ||
            (section ~ /^external-/ && section != "external-controller-unix")) exit 1
      }
      section ~ /^(mixed-port|bind-address|allow-lan|mode|log-level|ipv6|interface-name|external-controller-unix)$/ && /^  "/ {print}
      section ~ /^(tun|listeners)$/ { if ($0 != "}") print }
      section == "dns" && /^    "(enable|listen|ipv6|enhanced-mode)":/ {print}
    ' "$1" > updates.envelope.raw || return 1
    sed 's/,$//' updates.envelope.raw | sort
}
# Fixed pretty JSON arrays -> the exact rule projection returned by Mihomo /rules.
expected_rules() {
    awk '
      /^  "rules": \[/ {inside=1; next}
      inside && /^  \]/ {inside=0; next}
      inside {
        if ($0 !~ /^    "[^"\\]*"[,]?$/) exit 1
        line=$0; sub(/^    "/,"",line); sub(/"[,]?$/,"",line)
        n=split(line,a,","); type=toupper(a[1]); gsub(/-/,"",type)
        if (type == "MATCH") {if (n != 2) exit 1; print "MATCH\t\t" a[2]}
        else {
          if (n < 3 || n > 4 || (n == 4 && a[4] != "no-resolve")) exit 1
          if (type == "IPCIDR6") type="IPCIDR"
          if (type !~ /^(DOMAIN|DOMAINSUFFIX|DOMAINKEYWORD|DOMAINREGEX|GEOSITE|GEOIP|IPCIDR|SRCIPCIDR|SRCPORT|DSTPORT|PROCESSNAME|PROCESSPATH|NETWORK|RULESET|INNAME|INTYPE|INUSER|INPORT|IPASN|SRCIPASN|UID|DSCP)$/) exit 1
          if (type == "GEOIP") a[2]=tolower(a[2])
          print type "\t" a[2] "\t" a[3]
        }
        count++
      }
      END {if (!count) exit 1}
    ' "$1"
}
expected_groups() {
    awk '
      /^  "proxy-groups": \[/ {inside=1; next}
      inside && /^  \]/ {inside=0}
      inside && /^      "name": / {
        line=$0; sub(/^      "name": /,"",line); sub(/,$/,"",line)
        if (line !~ /^"[^"\\]+"$/) exit 1
        print line
      }
    ' "$1"
}
verify_loaded() {
    same_processes || return 1
    api rules > updates.rules.api || return 1
    api proxies > updates.proxies.api || return 1
    api group > updates.groups.api || return 1
    expected_rules "$1" > updates.rules.expected || return 1
    # /rules is a flat array of objects, emitted compactly by the pinned core.
    awk '
      {gsub(/\r/, ""); sub(/^\{"rules":\[/, ""); sub(/\]\}[[:space:]]*$/, "");
       n=split($0,rows,/\},\{/)
       for (i=1;i<=n;i++) {
         row=rows[i]; gsub(/^[{]|[}]$/, "", row)
         if (row !~ /"type":"[^"\\]*"/ || row !~ /"payload":"[^"\\]*"/ || row !~ /"proxy":"[^"\\]*"/) exit 1
         type=row; sub(/.*"type":"/,"",type); sub(/".*/,"",type)
         payload=row; sub(/.*"payload":"/,"",payload); sub(/".*/,"",payload)
         proxy=row; sub(/.*"proxy":"/,"",proxy); sub(/".*/,"",proxy)
         type=toupper(type); gsub(/-/,"",type)
         print type "\t" payload "\t" proxy
       }}
    ' updates.rules.api > updates.rules.actual || return 1
    cmp -s updates.rules.expected updates.rules.actual || return 1
    expected_groups "$1" > updates.groups.expected || return 1
    while IFS= read -r group; do
        grep -Fq "$group:" updates.proxies.api || return 1
    done < updates.groups.expected
    # The core also supplies GLOBAL. Compare the complete group name set, including removals.
    printf '"GLOBAL"\n' >> updates.groups.expected
    sort -u updates.groups.expected > updates.groups.sorted
    grep -o '"name":"[^"\\]*"' updates.groups.api > updates.groups.names || return 1
    sed 's/^"name"://' updates.groups.names | sort -u > updates.groups.actual
    cmp -s updates.groups.sorted updates.groups.actual || return 1
    same_processes
}
status_value() { sed -n "s/^$1=//p" update.status; }
write_status() {
    printf 'last_attempt=%s\nlast_success=%s\nreload_count=%s\nconfig_sha256=%s\nresult=%s\n' \
        "$attempt" "$success" "$reloads" "$confirmed" "$1" > update.status.tmp
    mv update.status.tmp update.status
}
load_status() {
    attempt=$(date +%s)
    success=$(status_value last_success)
    reloads=$(status_value reload_count)
    confirmed=$(status_value config_sha256)
    case "$reloads" in ''|*[!0-9]*) return 1;; esac
    [ "${#confirmed}" = 64 ]
}
cron_owned() {
    [ -f updates-crond.pid ] || return 1
    read cron_pid cron_birth < updates-crond.pid
    case "$cron_pid:$cron_birth" in *[!0-9:]*|:*) return 2;; esac
    [ -r "/proc/$cron_pid/stat" ] || return 1
    [ "$(readlink /proc/$cron_pid/exe)" = "$BB" ] || return 2
    [ "$(awk '{print $22}' /proc/$cron_pid/stat)" = "$cron_birth" ] || return 2
    tr '\000' '\n' < "/proc/$cron_pid/cmdline" > updates.cron-command
    printf '%s\n' "$BB" crond -f -c "$ROOT/cron" > updates.cron-expected
    cmp -s updates.cron-command updates.cron-expected || return 2
}
schedule() {
    [ -f updates.enabled ] || return 0
    mkdir -p cron || return 1
    printf '%s\n' "$CRON_LINE" > cron/root.new || return 1
    if ! cmp -s cron/root.new cron/root; then
        mv cron/root.new cron/root || return 1
    else rm cron/root.new || return 1
    fi
    if cron_owned; then return 0; fi
    # A reboot/reused PID leaves a stale record, never authority to signal that PID.
    rm -f updates-crond.pid || return 1
    (exec 9>&-; exec "$BB" crond -f -c "$ROOT/cron") > updates-crond.log 2>&1 </dev/null &
    cron_pid=$!
    echo "$cron_pid $(awk '{print $22}' /proc/$cron_pid/stat)" > updates-crond.pid || return 1
    sleep 1
    cron_owned
}
unschedule() {
    if cron_owned; then
        kill "$cron_pid" || return 1
        i=0
        while [ -r "/proc/$cron_pid/stat" ] && [ "$(awk '{print $22}' /proc/$cron_pid/stat)" = "$cron_birth" ]; do
            i=$((i+1)); [ "$i" -lt 10 ] || return 1; sleep 1
        done
    fi
    rm -f updates-crond.pid
}
updates_check() {
    private_root || return 20
    if [ ! -e updates.enabled ]; then
        [ ! -e updates.backup ] && [ ! -e updates.backup.new ] && [ ! -e updates.stage ] && \
            [ ! -e updates.pending ] && [ ! -e subscription.curl ] && \
            [ ! -e updates-crond.pid ] && [ ! -e cron ] || return 20
        return 10
    fi
    [ -d updates.backup ] && [ ! -e updates.pending ] || return 20
    case "$(status_value result)" in installed|changed|unchanged) :;; *) return 20;; esac
    [ "$(stat -c %u:%a subscription.curl)" = 0:600 ] || return 20
    [ "$(sha config.json)" = "$(status_value config_sha256)" ] || return 20
    [ "$(sha runtime.json)" = "$(status_value runtime_sha256)" ] || return 20
    cron_owned || return 20
    [ "$(cat cron/root)" = "$CRON_LINE" ] || return 20
    identity=$(identities) || return 20
    verify_loaded runtime.json || return 20
}
confirm_status() {
    write_status "$1"
    echo "runtime_sha256=$(sha runtime.json)" >> update.status
}
# Download and validate only; callers decide whether to install or just preflight.
prepare_candidate() {
    candidate_result=download-failed
    # No URL in argv/logs; curl config is root-only and contains an HTTPS URL.
    curl --config "$1" --silent --fail --location \
        --proto '=https' --proto-redir '=https' --max-redirs 3 --connect-timeout 15 --max-time 60 \
        --max-filesize 4194304 --proxy http://127.0.0.1:17890 --noproxy '' \
        --output updates.download 2>/dev/null || return 1
    candidate_result=invalid
    size=$(wc -c < updates.download)
    [ "$size" -gt 0 ] && [ "$size" -le 4194304 ] || return 1
    envelope updates.download > updates.envelope.next || return 1
    cmp -s "$2" updates.envelope.next || return 1
    candidate_result=uplink-failed
    interface=$(uplink) || return 1
    candidate_result=invalid
    render "$interface" updates.download > updates.runtime || return 1
    "$ROOT/mihomo" -t -d "$ROOT" -f "$ROOT/updates.runtime" >/dev/null 2>&1 && \
        expected_rules updates.runtime > updates.rules.expected && \
        expected_groups updates.runtime > updates.groups.expected
}
update_config() {
    private_root && [ -f updates.enabled ] && [ -e active ] || return 20
    [ ! -e updates.pending ] || return 20
    identity=$(identities) || return 20
    load_status || return 20
    [ "$(sha config.json)" = "$confirmed" ] && \
        [ "$(sha runtime.json)" = "$(status_value runtime_sha256)" ] || return 20
    trap 'rm -f updates.download updates.runtime updates.envelope.next' EXIT
    if ! prepare_candidate subscription.curl updates.envelope; then
        confirm_status "$candidate_result"; return 20
    fi
    next=$(sha updates.download)
    if [ "$next" = "$confirmed" ]; then
        verify_loaded runtime.json || { confirm_status verify-failed; return 20; }
        success=$attempt; confirm_status unchanged; return 0
    fi
    same_processes || return 20
    [ ! -e updates.previous-config ] && [ ! -e updates.previous-runtime ] || return 20
    printf '%s\n' "$identity" > updates.pending
    cp config.json updates.previous-config
    cp runtime.json updates.previous-runtime
    mv updates.runtime runtime.json
    mv updates.download config.json
    # Once issued, a failed/timeout response is ambiguous. Keep both versions until removal.
    code=$(curl --silent --max-time 20 --unix-socket "$ROOT/controller.sock" \
        -X PUT -H 'Content-Type: application/json' --data '{"path":"/data/adb/f50-soft-router/runtime.json"}' \
        -o updates.reload-response -w '%{http_code}' 'http://localhost/configs?force=false' 2>/dev/null) || {
        confirm_status reload-unknown; return 20;
    }
    if [ "$code" != 204 ] || ! verify_loaded runtime.json; then
        confirm_status reload-unknown; return 20
    fi
    confirmed=$next; success=$attempt; reloads=$((reloads+1))
    confirm_status changed
    rm updates.pending updates.previous-config updates.previous-runtime
}
# Called with a fully staged, host-verified baseline. Never invokes stop/start.
resume_updates() {
    [ ! -e updates.pending ] || return 20
    load_status && [ "$(sha config.json)" = "$confirmed" ] || return 20
    identity=$(identities) || return 20
    i=0
    until verify_loaded runtime.json; do i=$((i+1)); [ "$i" -lt 15 ] || return 20; sleep 1; done
    attempt=$(status_value last_attempt)
    confirm_status "$(status_value result)" || return 20
    schedule
}
updates_install() {
    private_root && [ ! -e updates.enabled ] && [ ! -e updates.backup ] || return 20
    identity=$(identities) || return 20
    [ "$identity" = "$(cat updates.stage/identity)" ] || return 20
    sha256sum -c updates.stage/baseline.sha >/dev/null 2>&1 || return 20
    if [ "$(cat updates.stage/boot-present)" = yes ]; then
        [ -f "$BOOT" ] && [ ! -L "$BOOT" ] || return 20
    else
        [ ! -e "$BOOT" ] && [ ! -L "$BOOT" ] || return 20
    fi
    [ ! -e updates.backup.new ] || return 20
    # A missing/invalid center artifact must fail before touching the running deployment.
    trap 'rm -f updates.download updates.runtime updates.envelope.next updates.envelope.raw updates.rules.expected updates.groups.expected' EXIT
    envelope updates.stage/config.json > updates.stage/envelope || return 20
    prepare_candidate updates.stage/subscription.curl updates.stage/envelope || return 20
    same_processes || return 20
    mkdir updates.backup.new
    for name in service.sh boot.sh deployment.json config.json runtime.json; do cp -p "$name" "updates.backup.new/$name"; done
    if [ -e "$BOOT" ]; then cp -p "$BOOT" updates.backup.new/boot-entry; fi
    mv updates.backup.new updates.backup
    for name in service.sh boot.sh updates.sh deployment.json subscription.curl; do
        cp "updates.stage/$name" "$name.new"; mv "$name.new" "$name"
    done
    chmod 700 service.sh boot.sh updates.sh
    chmod 600 subscription.curl deployment.json
    cp updates.stage/config.json config.json.new
    envelope config.json.new > updates.envelope || return 20
    interface=$(uplink) || return 20
    render "$interface" config.json.new > runtime.json.new || return 20
    "$ROOT/mihomo" -t -d "$ROOT" -f "$ROOT/runtime.json.new" >/dev/null 2>&1 || return 20
    expected_rules runtime.json.new > updates.rules.expected || return 20
    expected_groups runtime.json.new > updates.groups.expected || return 20
    printf '%s\n' "$identity" > updates.pending
    mv config.json.new config.json; mv runtime.json.new runtime.json
    same_processes || return 20
    owned mihomo && kill -HUP "$pid" || return 20
    i=0
    until verify_loaded runtime.json; do i=$((i+1)); [ "$i" -lt 15 ] || return 20; sleep 1; done
    same_processes || return 20
    attempt=$(date +%s); success=$attempt; reloads=0; confirmed=$(sha config.json)
    confirm_status installed
    if [ -f updates.backup/boot-entry ]; then cp boot.sh "$BOOT.new"; chmod 700 "$BOOT.new"; mv "$BOOT.new" "$BOOT"; fi
    touch updates.enabled
    rm updates.pending
    schedule || return 20
}
restore_baseline_files() {
    for name in config.json service.sh boot.sh deployment.json; do
        cp -p "updates.backup/$name" "$name.new" && mv "$name.new" "$name" || return 1
    done
    interface=$(uplink) || return 1
    render "$interface" updates.backup/config.json > runtime.json.new || return 1
    "$ROOT/mihomo" -t -d "$ROOT" -f "$ROOT/runtime.json.new" >/dev/null 2>&1 || return 1
    mv runtime.json.new runtime.json || return 1
    if [ -f updates.backup/boot-entry ]; then
        cp -p updates.backup/boot-entry "$BOOT.new" && mv "$BOOT.new" "$BOOT" || return 1
    fi
}
cleanup_updates() {
    rm -f updates.enabled updates.pending subscription.curl updates.sh updates.envelope update.status \
        updates.previous-config updates.previous-runtime controller.sock
    rm -rf updates.backup updates.stage cron
    rm -f updates.rules.api updates.proxies.api updates.rules.expected updates.rules.actual \
        updates.groups.expected updates.groups.api updates.groups.sorted updates.groups.names \
        updates.groups.actual updates.cron-command updates.cron-expected updates.envelope.raw \
        updates.reload-response updates-crond.log
}
updates_remove() {
    private_root || return 20
    if [ ! -e updates.backup ]; then
        [ ! -e updates.enabled ] && [ ! -e subscription.curl ] && [ ! -e updates.pending ] || return 20
        unschedule || return 20
        cleanup_updates
        rm -rf updates.backup.new
        return 0
    fi
    identity=$(identities) || return 20
    unschedule || return 20
    printf '%s\n' "$identity" > updates.pending
    # Even a failed HUP bootstrap must leave the original files on disk. Retain the
    # backup/pending marker when process-side verification cannot finish.
    trap restore_baseline_files EXIT
    restore_baseline_files || return 20
    # First verify the original policy through the private controller, then remove it.
    # This also recovers a bootstrap failure that never created the socket.
    awk 'NR == 1 {print; print "  \"external-controller-unix\": \"/data/adb/f50-soft-router/controller.sock\","; next} {print}' \
        runtime.json > runtime.json.new
    "$ROOT/mihomo" -t -d "$ROOT" -f "$ROOT/runtime.json.new" >/dev/null 2>&1 || return 20
    mv runtime.json.new runtime.json
    same_processes || return 20
    owned mihomo && kill -HUP "$pid" || return 20
    i=0
    until verify_loaded runtime.json; do i=$((i+1)); [ "$i" -lt 15 ] || return 20; sleep 1; done
    restore_baseline_files || return 20
    same_processes || return 20
    owned mihomo && kill -HUP "$pid" || return 20
    i=0
    while api version >/dev/null 2>&1; do i=$((i+1)); [ "$i" -lt 15 ] || return 20; sleep 1; done
    sleep 1
    same_processes || return 20
    trap - EXIT
    # Host checks the restored baseline before releasing recovery material.
}
updates_finalize() {
    private_root || return 20
    if [ ! -e updates.backup ]; then
        if updates_check; then return 20; else code=$?; [ "$code" = 10 ]; return; fi
    fi
    identity=$(identities) || return 20
    [ "$identity" = "$(cat updates.pending)" ] || return 20
    for name in config.json service.sh boot.sh deployment.json; do
        cmp -s "updates.backup/$name" "$name" || return 20
    done
    if [ -f updates.backup/boot-entry ]; then cmp -s updates.backup/boot-entry "$BOOT" || return 20; fi
    interface=$(uplink) || return 20
    render "$interface" updates.backup/config.json > runtime.json.new || return 20
    cmp -s runtime.json.new runtime.json || return 20
    rm runtime.json.new
    if api version >/dev/null 2>&1; then return 20; fi
    cleanup_updates
}
