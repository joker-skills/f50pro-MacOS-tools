#!/usr/bin/env bash
# Sample, every 0.5 s for N seconds, which ZTE/Spreadtrum/Google-AOA USB device ids are present on this Mac
# and which processes hold IOKit user clients on them. Output: one line per change.
SECONDS_TOTAL="${1:-420}"
end=$((SECONDS + SECONDS_TOTAL))
last=""
while [ $SECONDS -lt $end ]; do
  snap=$(ioreg -p IOUSB -l -w0 2>/dev/null | python3 -c '
import sys,re
t=sys.stdin.read()
out=[]
for d in re.split(r"\n(?=\s*\+-o )",t):
    v=re.search(r"\"idVendor\" = (\d+)",d); p=re.search(r"\"idProduct\" = (\d+)",d)
    if v and p and int(v.group(1)) in (0x19d2,0x1782,0x18d1):
        uc=sorted(set(re.findall(r"IOUserClientCreator\" = \"pid \d+, ([^\"]+)",d)))
        out.append("%04x:%04x[%s]"%(int(v.group(1)),int(p.group(1)),",".join(uc)))
print(" ".join(out) if out else "ABSENT")')
  if [ "$snap" != "$last" ]; then echo "$(date +%T.%N | cut -c1-12) $snap"; last="$snap"; fi
  sleep 0.5
done
