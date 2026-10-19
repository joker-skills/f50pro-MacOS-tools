#!/data/adb/magisk/busybox ash
# Internal authentication helper; never starts or signals a vendor process.
umask 077
P=/data/adb/f50-soft-router
A=$P/auth
G=$P/auth.recovery
B=/data/adb/service.d/f50-auth-hook.sh
BB=/data/adb/magisk/busybox
FILES='frida-inject auth-hook.bundle.js auth-service.sh boot-entry source_oid'
WORK_FILES='frida-inject auth-hook.bundle.js auth-service.sh source_oid'
RUNTIME_FILES='enabled supervisor.identity injector.identity target.identity session status.json status.new check.request check.new session.log supervisor.log'
INJECTOR_SHA=5358e6397ac5983c767b2137d4dd3e1f20af70f401d93075c99c846c7f09fe2c

fail() { echo "auth: $*" >&2; return 20; }
private_dir() { [ -d "$1" ] && [ ! -L "$1" ] && [ "$(stat -c %u:%a "$1")" = 0:700 ]; }
regular() { [ -f "$1" ] && [ ! -L "$1" ] && [ "$(stat -c %u:%a "$1")" = "0:$2" ]; }
runtime_safe() {
    private_dir "$A" || return 20
    for name in $RUNTIME_FILES; do
        if [ -e "$A/$name" ] || [ -L "$A/$name" ]; then regular "$A/$name" 600 || return 20; fi
    done
    if [ -e "$A/stdin" ] || [ -L "$A/stdin" ]; then
        [ -p "$A/stdin" ] && [ ! -L "$A/stdin" ] && [ "$(stat -c %u:%a "$A/stdin")" = 0:600 ] || return 20
    fi
}
sha() { sha256sum "$1" | cut -d ' ' -f 1; }
mode() { case "$1" in frida-inject|auth-service.sh|boot-entry) echo 700;; *) echo 600;; esac; }
expected() { sed -n "s/  $1\$/ /p" "$G/manifest" | tr -d ' '; }
nonce_new() { tr -d '-' < /proc/sys/kernel/random/uuid; }

# Fingerprint cache avoids hashing the 84 MB ELF every 30 seconds. Full hash on
# startup, metadata change or relaunch; this is corruption recovery, not root tamper protection.
valid_file() {
    regular "$1" "$2" || return 1
    if [ "${1##*/}" = frida-inject ]; then
        fp=$(stat -c '%i:%s:%Y:%Z:%u:%a' "$1") || return 1
        case "$1:$fp" in "$gold_cache"|"$work_cache") return 0;; esac
        [ "$(sha "$1")" = "$INJECTOR_SHA" ] || return 1
        if [ "$1" = "$G/frida-inject" ]; then gold_cache=$1:$fp; else work_cache=$1:$fp; fi
    else
        [ "$(sha "$1")" = "$3" ] || return 1
    fi
}
golden() {
    private_dir "$G" && regular "$G/manifest" 600 && regular "$G/manifest.sha256" 600 || return 20
    [ "$(sha "$G/manifest")" = "$(cat "$G/manifest.sha256")" ] || return 20
    [ "$(wc -l < "$G/manifest" | tr -d ' ')" = 5 ] || return 20
    for name in $FILES; do
        sum=$(expected "$name")
        [ "${#sum}" = 64 ] && ! echo "$sum" | grep -q '[^a-f0-9]' || return 20
        [ "$name" != frida-inject ] || [ "$sum" = "$INJECTOR_SHA" ] || return 20
        valid_file "$G/$name" "$(mode "$name")" "$sum" || return 20
    done
    oid=$(cat "$G/source_oid")
    [ "${#oid}" = 40 ] && ! echo "$oid" | grep -q '[^a-f0-9]' || return 20
}
heal() {
    golden || { fail 'invalid recovery copy; keeping all material'; return 20; }
    if [ ! -e "$A" ] && [ ! -L "$A" ]; then mkdir "$A" || return 20; fi
    [ -d "$A" ] && [ ! -L "$A" ] && [ "$(stat -c %u "$A")" = 0 ] || return 20
    chmod 700 "$A" || return 20
    for name in $FILES; do
        dest=$A/$name
        [ "$name" != boot-entry ] || dest=$B
        if ! valid_file "$dest" "$(mode "$name")" "$(expected "$name")"; then
            # Rename replaces a corrupt file or symlink, never follows it.
            [ ! -d "$dest" ] || return 20
            [ ! -e "$dest.auth-new" ] && [ ! -L "$dest.auth-new" ] || return 20
            cp "$G/$name" "$dest.auth-new" && chmod "$(mode "$name")" "$dest.auth-new" &&
                mv -f "$dest.auth-new" "$dest" || return 20
        fi
    done
}
identity() {
    case "$1" in ''|*[!0-9]*) return 1;; esac
    [ -r "/proc/$1/stat" ] && [ -r "/proc/$1/cmdline" ] || return 1
    statline=$(cat "/proc/$1/stat") || return 1
    rest=${statline##*) }
    [ "${rest%% *}" != Z ] || return 1
    start=$(echo "$rest" | awk '{print $20}')
    cmd=$(sha "/proc/$1/cmdline")
    [ -n "$start" ] && [ -n "$cmd" ] || return 1
    echo "$1 $start $cmd"
}
same() { [ -n "$1" ] && [ "$(identity "${1%% *}")" = "$1" ]; }
injector_alive() {
    same "$injector" || return 20
    case "$(readlink "/proc/${injector%% *}/exe")" in "$A/frida-inject"|"$A/frida-inject (deleted)") return 0;; esac
    return 20
}
id_json() {
    if [ -z "$1" ]; then echo null; else
        set -- $1
        printf '{"pid":%s,"starttime":"%s","cmdline_sha256":"%s"}' "$1" "$2" "$3"
    fi
}
target() {
    found=
    pids=$(/system/bin/pidof com.zte.web)
    result=$?
    [ "$result" -le 1 ] || return 20
    for pid in $pids; do
        case "$pid" in ''|*[!0-9]*) return 20;; esac
        # Android pads argv[0] with NULs. Keep the complete bytes in identity,
        # but accept only that exact first argument followed entirely by NULs.
        [ "$(tr -d '\000' < "/proc/$pid/cmdline"; printf .)" = com.zte.web. ] || return 20
        candidate=$(identity "$pid") || return 20
        [ -z "$found" ] || return 20
        found=$candidate
    done
    [ -n "$found" ] || return 10
    echo "$found"
}
no_other_injector() {
    pids=$(/system/bin/pidof frida-inject)
    result=$?
    [ "$result" -le 1 ] || return 20
    for pid in $pids; do
        current=$(identity "$pid") || return 20
        [ -n "$injector" ] && [ "$current" = "$injector" ] && injector_alive || return 20
    done
}
idle() {
    injector=
    no_other_injector || return 20
    # Every supervisor holds auth.lock; no process-table scan is needed.
    if [ -e "$P/auth.lock" ] || [ -L "$P/auth.lock" ]; then
        regular "$P/auth.lock" 600 || return 20
        exec 9< "$P/auth.lock"
        "$BB" flock -n 9 || return 20
    fi
}
absent() {
    for path in "$A" "$G" "$B" "$P/auth.recovery.stage"; do
        [ ! -e "$path" ] && [ ! -L "$path" ] || return 20
    done
    idle
}
uninstalled() {
    echo '{"source_oid":null,"state":"uninstalled","target":null,"supervisor":null,"injector":null,"probe":{"passed":false,"fresh":false},"files_healthy":true}'
}
write_status() {
    private_dir "$A" || return 20
    for path in "$A/status.new" "$A/status.json"; do
        if [ -e "$path" ] || [ -L "$path" ]; then regular "$path" 600 || return 20; fi
    done
    printf '{"source_oid":"%s","state":"%s","target":%s,"supervisor":%s,"injector":%s,"probe":{"nonce":"%s","passed":%s},"files_healthy":%s}\n' \
        "$oid" "$1" "$(id_json "$target_id")" "$(id_json "$supervisor")" \
        "$(id_json "$injector")" "${request:-}" "${passed:-false}" "${files_ok:-false}" > "$A/status.new" &&
        mv "$A/status.new" "$A/status.json"
}
wait_event() {
    event=$1 active_value=$2 returned=$3
    count=0
    while [ "$count" -lt 10 ]; do
        injector_alive && same "$target_id" || return 20
        pattern="^\{\"event\":\"$event\",\"pid\":${target_id%% *},\"active\":$active_value,\"calls\":[0-9]+,\"session\":\"$session\",\"nonce\":\"$request\"$returned\}$"
        grep -Eq "$pattern" "$A/session.log" && return 0
        count=$((count + 1)); sleep 1
    done
    return 20
}
probe() {
    passed=false
    injector_alive && [ "$(target)" = "$target_id" ] && no_other_injector || return 20
    : > "$A/session.log"
    printf 'probe %s\n' "$request" >&7 || return 20
    wait_event probe-passed true ',"returned":false' || return 20
    passed=true
}
natural_exit() {
    # Only a missing/zombie direct child is a confirmed exit. PID reuse is unknown.
    [ -n "$injector" ] || return 0
    child=${injector%% *}
    if [ -e "/proc/$child" ]; then
        line=$(cat "/proc/$child/stat") || return 20
        rest=${line##*) }
        [ "${rest%% *}" = Z ] && [ "$(echo "$rest" | awk '{print $20}')" = "$(echo "$injector" | cut -d ' ' -f 2)" ] || return 20
    fi
    wait "$child" 2>/dev/null
    injector=
    exec 7>&-
    return 0
}
detach() {
    [ -n "$injector" ] || return 0
    if ! injector_alive; then natural_exit; return $?; fi
    same "$target_id" && no_other_injector || return 20
    request=$(nonce_new)
    : > "$A/session.log"
    printf 'restore %s\n' "$request" >&7 || return 20
    wait_event restored false '' || return 20
    injector_alive || return 20
    kill -TERM "${injector%% *}" || return 20
    count=0
    while [ "$count" -lt 10 ]; do
        natural_exit && return 0
        count=$((count + 1)); sleep 1
    done
    return 20
}
launch() {
    [ "$(/system/bin/getprop sys.boot_completed)" = 1 ] || return 10
    target_id=$(target) || return $?
    no_other_injector || return 20
    gold_cache= work_cache=
    heal || return 20
    [ "$(target)" = "$target_id" ] || return 20
    [ ! -e "$A/stdin" ] && [ ! -L "$A/stdin" ] || {
        [ -p "$A/stdin" ] && [ ! -L "$A/stdin" ] || return 20
        rm "$A/stdin" || return 20
    }
    mkfifo -m 600 "$A/stdin" || return 20
    # RDWR keeps stdin open without an ADB writer. Child only inherits fd 0.
    exec 7<> "$A/stdin" || return 20
    : > "$A/session.log"
    session=$(nonce_new)
    request=
    parameters="{\"flush\":true,\"session\":\"$session\"}"
    ( exec 9>&- 8>&-
      exec "$A/frida-inject" -p "${target_id%% *}" -i -s "$A/auth-hook.bundle.js" -P "$parameters" <&7 7>&- >> "$A/session.log" 2>&1 ) &
    child=$!
    # The fork may still be between ash and exec; wait for the exact ELF/argv.
    expected_cmd=$(printf '%s\000' "$A/frida-inject" -p "${target_id%% *}" -i -s "$A/auth-hook.bundle.js" -P "$parameters" | sha256sum | cut -d ' ' -f 1)
    count=0
    while [ "$count" -lt 5 ]; do
        injector=$(identity "$child")
        [ "${injector##* }" = "$expected_cmd" ] && [ "$(readlink "/proc/$child/exe")" = "$A/frida-inject" ] && break
        sleep 1; count=$((count + 1))
    done
    [ "${injector##* }" = "$expected_cmd" ] || return 20
    printf '%s\n' "$injector" > "$A/injector.identity"
    printf '%s\n' "$target_id" > "$A/target.identity"
    printf '%s\n' "$session" > "$A/session"
    wait_event installed true '' || return 20
    request=$(nonce_new)
    probe
}
run() {
    runtime_safe || return 20
    exec 9> "$P/auth.lock"
    "$BB" flock -n 9 || return 20
    supervisor=$(identity $$)
    printf '%s\n' "$supervisor" > "$A/supervisor.identity" || return 20
    injector= target_id= oid= request= passed=false files_ok=false
    trap 'rm -f "$A/enabled"; stopping=yes' TERM INT HUP
    stopping= failures=0 tick=30
    while :; do
        if [ ! -f "$A/enabled" ] || [ -L "$A/enabled" ] || [ -n "$stopping" ]; then
            detach || { write_status restore-unconfirmed; return 20; }
            passed=false; write_status stopped; return 0
        fi
        if [ "$tick" -ge 30 ] || { [ -n "$injector" ] && [ -f "$A/check.request" ]; }; then
            passed=false
            files_ok=false
            heal || { write_status recovery-invalid; return 20; }
            files_ok=true
            runtime_safe || { write_status runtime-unsafe; return 20; }
            if [ -n "$injector" ] && ! injector_alive; then
                natural_exit || { write_status identity-unknown; return 20; }
                failures=$((failures + 1))
            fi
            if [ "$failures" -ge 3 ]; then write_status retry-limit; return 20; fi
            if [ -z "$injector" ]; then
                launch
                result=$?
                if [ "$result" -ne 0 ]; then
                    if [ "$result" = 10 ]; then write_status waiting-target
                    elif natural_exit; then failures=$((failures + 1)); write_status retrying
                    else write_status injection-unconfirmed; return 20; fi
                    tick=0; sleep 1; continue
                fi
            fi
            request=$(nonce_new)
            if [ -f "$A/check.request" ]; then
                request=$(cat "$A/check.request"); rm "$A/check.request"
                [ "${#request}" = 32 ] && ! echo "$request" | grep -q '[^a-f0-9]' || return 20
            fi
            probe || { write_status probe-unconfirmed; return 20; }
            failures=0
            write_status healthy || return 20
            tick=0
        fi
        sleep 1; tick=$((tick + 1))
    done
}
start() {
    heal || return 20
    runtime_safe || return 20
    if [ -f "$A/supervisor.identity" ] && same "$(cat "$A/supervisor.identity")"; then return 0; fi
    injector=
    no_other_injector || return 20
    [ ! -L "$A/enabled" ] && [ ! -d "$A/enabled" ] || return 20
    : > "$A/enabled"
    # One detached supervisor; neither the injector nor launcher holds its lock.
    ( exec "$BB" nohup "$BB" ash "$G/auth-service.sh" run </dev/null > "$A/supervisor.log" 2>&1 8>&- 9>&- ) &
}
check() {
    runtime_safe || return 20
    if ! regular "$A/supervisor.identity" 600 || ! same "$(cat "$A/supervisor.identity")"; then
        [ ! -f "$A/status.json" ] || cat "$A/status.json"
        return 20
    fi
    request=$(nonce_new)
    printf '%s\n' "$request" > "$A/check.new" && mv "$A/check.new" "$A/check.request" || return 20
    count=0
    while [ "$count" -lt 45 ]; do
        if grep -q "\"nonce\":\"$request\",\"passed\":true" "$A/status.json" 2>/dev/null; then
            same "$(cat "$A/supervisor.identity")" && [ -f "$A/enabled" ] || return 20
            cat "$A/status.json"; return 0
        fi
        sleep 1; count=$((count + 1))
    done
    [ ! -f "$A/status.json" ] || cat "$A/status.json"
    return 20
}
stop() {
    # Revoke first: healing must never recreate enabled.
    [ ! -L "$A" ] || return 20
    rm -f "$A/enabled" || return 20
    count=0
    while [ -f "$A/supervisor.identity" ] && same "$(cat "$A/supervisor.identity")"; do
        [ "$count" -lt 45 ] || return 20
        sleep 1; count=$((count + 1))
    done
    injector=
    runtime_safe || return 20
    if [ -f "$A/injector.identity" ]; then
        injector=$(cat "$A/injector.identity")
        target_id=$(cat "$A/target.identity")
        session=$(cat "$A/session")
        [ "${#session}" = 32 ] && ! echo "$session" | grep -q '[^a-f0-9]' || return 20
        if [ -e "/proc/${injector%% *}" ]; then
            injector_alive || return 20
            exec 7<> "$A/stdin" || return 20
            detach || return 20
        fi
    fi
    injector=
    no_other_injector || return 20
    if [ -e "$B" ] || [ -L "$B" ]; then
        regular "$B" 700 && [ "$(sha "$B")" = "$(expected boot-entry)" ] || return 20
        rm "$B" || return 20
    fi
}
known_entries() {
    for path in "$1"/* "$1"/.[!.]* "$1"/..?*; do
        [ -e "$path" ] || [ -L "$path" ] || continue
        case " $2 " in *" ${path##*/} "*) ;; *) fail 'unknown cleanup entry; keeping material'; return 20;; esac
        [ ! -L "$path" ] && [ ! -d "$path" ] || return 20
    done
}
restore_check() {
    golden && [ "$(sha "$G/manifest")" = "$1" ] && [ "$oid" = "$2" ] || return 20
    [ ! -e "$P/auth.recovery.stage" ] && [ ! -L "$P/auth.recovery.stage" ] || return 20
    runtime_safe && known_entries "$A" "$WORK_FILES $RUNTIME_FILES stdin" &&
        known_entries "$G" "$FILES manifest manifest.sha256" || return 20
    for name in $FILES; do
        path=$A/$name
        [ "$name" != boot-entry ] || path=$B
        valid_file "$path" "$(mode "$name")" "$(expected "$name")" || return 20
    done
    for name in supervisor.identity injector.identity target.identity; do
        if [ -f "$A/$name" ]; then
            saved=$(cat "$A/$name")
            set -- $saved
            [ "$#" = 3 ] || return 20
            case "$1$2" in *[!0-9]*) return 20;; esac
            [ "${#3}" = 64 ] && ! echo "$3" | grep -q '[^a-f0-9]' || return 20
            if [ -e "/proc/${saved%% *}" ]; then same "$saved" || return 20; fi
        fi
    done
    if [ -e "$P/auth.lock" ]; then
        exec 9< "$P/auth.lock"
        if ! "$BB" flock -n 9; then
            regular "$A/supervisor.identity" 600 && same "$(cat "$A/supervisor.identity")" || return 20
        fi
    fi
    injector=
    if [ -f "$A/injector.identity" ]; then injector=$(cat "$A/injector.identity"); fi
    no_other_injector || return 20
    return 10
}
remove() {
    absent && { uninstalled; return 0; }
    golden && [ "$(sha "$G/manifest")" = "$1" ] && [ "$oid" = "$2" ] || return 20
    stop && idle || return 20
    [ ! -e "$P/auth.recovery.stage" ] && [ ! -L "$P/auth.recovery.stage" ] || return 20
    runtime_safe && known_entries "$A" "$WORK_FILES $RUNTIME_FILES stdin" &&
        known_entries "$G" "$FILES manifest manifest.sha256" || return 20
    # Validate the whole deletion set before removing any running asset.
    for name in $WORK_FILES; do
        if [ -e "$A/$name" ] || [ -L "$A/$name" ]; then
            valid_file "$A/$name" "$(mode "$name")" "$(expected "$name")" || return 20
        fi
    done
    for name in $WORK_FILES $RUNTIME_FILES stdin; do
        if [ -e "$A/$name" ]; then rm "$A/$name" || return 20; fi
    done
    rmdir "$A" || return 20
    for name in $FILES manifest.sha256 manifest; do rm "$G/$name" || return 20; done
    rmdir "$G" || return 20
    absent && uninstalled
}
install() {
    expected_manifest=$1
    injector=
    if private_dir "$A" && regular "$A/supervisor.identity" 600 && same "$(cat "$A/supervisor.identity")" && regular "$A/injector.identity" 600; then
        injector=$(cat "$A/injector.identity")
    fi
    no_other_injector || { fail 'existing injector; restore and exit that session first'; return 20; }
    stage=$P/auth.recovery.stage
    [ ! -e "$stage" ] && [ ! -L "$stage" ] || return 20
    mkdir -m 700 "$stage" && "$BB" tar -xf - -C "$stage" || return 20
    saved_g=$G; G=$stage
    golden && [ "$(sha "$G/manifest")" = "$expected_manifest" ] || return 20
    G=$saved_g
    if [ -e "$G" ] || [ -L "$G" ]; then
        # Only an identical known install can be resumed; no unknown overwrite.
        private_dir "$G" && regular "$G/manifest" 600 && [ "$(sha "$G/manifest")" = "$expected_manifest" ] || return 20
        # A verified host backup may repair this known golden copy, never work files.
        for name in $FILES manifest.sha256; do
            if ! regular "$G/$name" "$(mode "$name")" || [ "$(sha "$G/$name")" != "$(sha "$stage/$name")" ]; then
                [ ! -d "$G/$name" ] && [ ! -e "$G/$name.auth-new" ] && [ ! -L "$G/$name.auth-new" ] || return 20
                cp "$stage/$name" "$G/$name.auth-new" && chmod "$(mode "$name")" "$G/$name.auth-new" && mv -f "$G/$name.auth-new" "$G/$name" || return 20
            fi
        done
        golden || return 20
        for name in $FILES manifest manifest.sha256; do rm "$stage/$name" || return 20; done
        rmdir "$stage" || return 20
    else
        [ ! -e "$A" ] && [ ! -L "$A" ] && [ ! -e "$B" ] && [ ! -L "$B" ] || return 20
        mv "$stage" "$G" || return 20
    fi
    start
}
main() {
    control_locked=
    [ ! -L "${B%/*}" ] || return 20
    if [ -e "$P" ] || [ -L "$P" ]; then private_dir "$P" || return 20; fi
    for lock in "$P/auth.lock" "$P/auth.control.lock"; do
        if [ -e "$lock" ] || [ -L "$lock" ]; then regular "$lock" 600 || return 20; fi
    done
    case "$1" in
        check|restore-check|remove)
            # Queries do not create or truncate any device lock or asset.
            if [ -e "$P/auth.control.lock" ]; then
                exec 8< "$P/auth.control.lock"
                "$BB" flock -n 8 || return 20
                control_locked=yes
            fi
            if absent; then
                uninstalled
                [ "$1" = check ] && return 10
                return 0
            fi
            case "$1" in
                check) check; return $?;;
                restore-check) restore_check "$2" "$3"; return $?;;
            esac;;
    esac
    private_dir "$P" && [ -d "${B%/*}" ] || return 20
    if [ "$1" = run ]; then run; return $?; fi
    if [ -z "$control_locked" ]; then
        exec 8> "$P/auth.control.lock"
        "$BB" flock -n 8 || return 20
    fi
    case "$1" in
        install) install "$2";;
        remove) remove "$2" "$3";;
        stop) [ ! -L "$A" ] && rm -f "$A/enabled" && golden && stop;;
        *) fail 'unknown action';;
    esac
}
main "$@"
