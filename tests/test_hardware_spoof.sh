#!/usr/bin/env bash
# Tests for scripts/docker/hardware-spoof.sh — run with: bash tests/test_hardware_spoof.sh
# Uses CHROME_CONFIG_DIR to redirect the signature file away from the container path.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

SIG="$TMP/hardware-signature.json"
export CHROME_CONFIG_DIR="$TMP"
export CHROME_SPOOF_DIR="$TMP/spoof"
# No LD_PRELOAD shim on the host: the generator must still work without it.
export CHROME_SPOOF_LIB="$TMP/nonexistent-libhwspoof.so"

# ---------------------------------------------------------------- schema ----
# The blueprint table lives inside the generator; these assertions encode the
# invariant the table must satisfy, so a bad edit fails the suite.
cat > "$TMP/schema.py" <<'PYEOF'
import json, re, sys
d = json.load(open(sys.argv[1]))
hw, sysd, br = d['hardware'], d['system'], d['browser']

for k in ('cpu_model', 'cpu_vendor_id', 'cpu_physical_cores', 'cpu_cores',
          'mem_total_gb', 'mac_address', 'screen_resolution'):
    assert k in hw, 'missing hardware.%s' % k
for k in ('sys_vendor', 'product_name', 'board_vendor'):
    assert hw['dmi'].get(k), 'missing hardware.dmi.%s' % k
for k in ('sysname', 'release', 'machine'):
    assert sysd['uname'].get(k), 'missing system.uname.%s' % k
for k in ('timezone',):
    assert sysd[k], 'missing system.%s' % k
for k in ('language', 'gpu_mode'):
    assert br[k], 'missing browser.%s' % k
assert d['schema_version'] == 2, d.get('schema_version')

# ---- the core invariant: the model and the core count must agree -----------
model = hw['cpu_model']
phys, log = hw['cpu_physical_cores'], hw['cpu_cores']
assert isinstance(phys, int) and isinstance(log, int), (phys, log)
assert 1 <= phys <= 64, phys
# cpu_cores is the LOGICAL count (what taskset/sched_getaffinity pins) and can
# never be below the physical count.
assert phys <= log, 'logical %d < physical %d' % (log, phys)
# SMT exposes at most one extra thread per physical core, and always in pairs
# (4C/4T, 4C/6T and 4C/8T are all real shipping configurations). Anything
# outside that range would be an obvious tell.
assert log <= phys * 2, 'logical %d exceeds 2x physical %d' % (log, phys)
assert (log - phys) % 2 == 0, 'threads %d for %d cores is not a real SMT config' % (log, phys)
# AMD encodes the physical count in the string; it must match.
m = re.search(r'(\d+)-Core', model)
if m:
    assert int(m.group(1)) == phys, \
        'model %r says %s cores but signature says %d' % (model, m.group(1), phys)
# vendor_id must match the model vendor.
want = 'AuthenticAMD' if model.startswith('AMD') else 'GenuineIntel'
assert hw['cpu_vendor_id'] == want, (hw['cpu_vendor_id'], model)

# ---- other coherence ------------------------------------------------------
assert hw['mem_total_gb'] in (8, 16, 32, 64), hw['mem_total_gb']
mac = hw['mac_address']
assert re.fullmatch(r'([0-9a-f]{2}:){5}[0-9a-f]{2}', mac), mac
first = int(mac.split(':')[0], 16)
# Locally administered (bit 1 set) and unicast (bit 0 clear): can never be a
# real vendor OUI and is what a tun/veth-style interface looks like.
assert first & 0x02, 'MAC is not locally administered: %s' % mac
assert not first & 0x01, 'MAC is multicast: %s' % mac
assert hw['screen_resolution'] in ('1920x1080', '2560x1440', '1366x768', '1440x900')
assert re.fullmatch(r'\d+\.\d+\.\d+-\d+-generic', sysd['uname']['release']), sysd['uname']
# SMBIOS exposes chassis_type as a number, not a word.
assert hw['dmi']['chassis_type'].isdigit(), hw['dmi']['chassis_type']
PYEOF

# 1. First run generates a full signature
bash "$ROOT/scripts/docker/hardware-spoof.sh" testprof >/dev/null
[[ -f "$SIG" ]] || { echo "FAIL: signature not created"; exit 1; }
python3 "$TMP/schema.py" "$SIG"
echo "signature schema + coherence OK"

# 1b. The faked sysfs/procfs tree the shim redirects onto must exist and be
#     internally consistent with the signature.
python3 - "$SIG" "$CHROME_SPOOF_DIR" <<'PYEOF'
import json, os, re, sys
d = json.load(open(sys.argv[1])); root = sys.argv[2]
hw = d['hardware']
def rd(p):
    with open(os.path.join(root, p)) as f: return f.read()

cpu = rd('proc/cpuinfo')
assert cpu.count('processor\t:') == hw['cpu_cores'], (cpu.count('processor\t:'), hw['cpu_cores'])
assert ('model name\t: %s\n' % hw['cpu_model']) in cpu, hw['cpu_model']
assert ('vendor_id\t: %s\n' % hw['cpu_vendor_id']) in cpu
assert ('cpu cores\t: %d\n' % hw['cpu_physical_cores']) in cpu
assert ('siblings\t: %d\n' % hw['cpu_cores']) in cpu

mem = rd('proc/meminfo')
total = int(re.search(r'MemTotal:\s+(\d+) kB', mem).group(1))
assert total == hw['mem_total_gb'] * 1024 * 1024, (total, hw['mem_total_gb'])
# Every kB line must be a plausible share of MemTotal.
for name, val in re.findall(r'^(\w+):\s+(\d+) kB', mem, re.M):
    assert int(val) <= total, '%s=%s exceeds MemTotal=%d' % (name, val, total)
for k in ('product_name', 'board_vendor', 'sys_vendor'):
    assert rd('sys/class/dmi/id/' + k).strip() == hw['dmi'][k], k
assert d['system']['uname']['release'] in rd('proc/version')
print("faked /proc + /sys tree OK")
PYEOF

# 2. Second run is stable — no regeneration
cp "$SIG" "$TMP/sig1.json"
bash "$ROOT/scripts/docker/hardware-spoof.sh" testprof >/dev/null
diff "$TMP/sig1.json" "$SIG" >/dev/null \
  && echo "stability OK" \
  || { echo "FAIL: signature changed between runs"; exit 1; }

# 3. Backfill — a field removed from an old signature is regenerated
python3 - "$SIG" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
del d['browser']['gpu_mode']
del d['hardware']['mem_total_gb']
del d['hardware']['dmi']
json.dump(d, open(sys.argv[1], 'w'))
PYEOF
bash "$ROOT/scripts/docker/hardware-spoof.sh" testprof >/dev/null
python3 "$TMP/schema.py" "$SIG"
echo "backfill OK"

# 4. Exported values match the stored signature
OUT="$(bash -c 'source "$0" testprof >/dev/null; echo "$CHROME_CPU_CORES|$CHROME_RESOLUTION|$CHROME_TIMEZONE|$CHROME_LANGUAGE|$CHROME_GPU_MODE|$CHROME_CPU_MODEL|$CHROME_CPU_PHYSICAL_CORES|$CHROME_MEM_TOTAL_GB|$CHROME_MEM_TOTAL_KB|$CHROME_MAC_ADDRESS|$CHROME_DMI_PRODUCT_NAME|$CHROME_KERNEL_RELEASE"' "$ROOT/scripts/docker/hardware-spoof.sh")"
python3 - "$SIG" "$OUT" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1])); hw, sysd, br = d['hardware'], d['system'], d['browser']
f = sys.argv[2].split('|')
cores, res, tz, lang, gpu, model, phys, gb, kb, mac, pn, rel = f
assert int(cores) == hw['cpu_cores'], (cores, hw)
assert res == hw['screen_resolution']
assert tz == sysd['timezone']
assert lang == br['language']
assert gpu == br['gpu_mode']
assert model == hw['cpu_model']
assert int(phys) == hw['cpu_physical_cores']
assert int(gb) == hw['mem_total_gb']
assert int(kb) == hw['mem_total_gb'] * 1024 * 1024
assert mac == hw['mac_address']
assert pn == hw['dmi']['product_name']
assert rel == sysd['uname']['release']
# The five-value contract is the first line and must stay space-free so the
# `read -r A B C D E <<< "$(...)"` call site keeps working.
assert all(' ' not in v for v in f[:5]), f[:5]
print("exports OK")
PYEOF

# 5. Legacy signature (pre-T4, only cpu_cores) is upgraded in place and the
#    pre-existing identity is preserved.
for legacy in 14 11 7 8 6 4; do
  rm -rf "$TMP/legacy"; mkdir -p "$TMP/legacy"
  cat > "$TMP/legacy/hardware-signature.json" <<EOF
{
  "profile": "Fiverr-Demo",
  "hardware": { "cpu_cores": $legacy, "screen_resolution": "1440x900" },
  "system": { "timezone": "Africa/Cairo" },
  "browser": { "language": "ur-PK", "gpu_mode": "swiftshader" }
}
EOF
  CHROME_CONFIG_DIR="$TMP/legacy" CHROME_SPOOF_DIR="$TMP/legacy/spoof" \
      bash "$ROOT/scripts/docker/hardware-spoof.sh" Fiverr-Demo >/dev/null
  python3 - "$TMP/legacy/hardware-signature.json" "$legacy" <<'PYEOF'
import json, re, sys
d = json.load(open(sys.argv[1])); legacy = int(sys.argv[2]); hw = d['hardware']
# Identity of an existing profile must survive the upgrade.
assert d['profile'] == 'Fiverr-Demo', d
assert hw['screen_resolution'] == '1440x900', hw
assert d['system']['timezone'] == 'Africa/Cairo', d
assert d['browser']['language'] == 'ur-PK', d
assert d['browser']['gpu_mode'] == 'swiftshader', d
# Coherence: the model must now agree with the core count.
phys, log = hw['cpu_physical_cores'], hw['cpu_cores']
assert phys <= log <= phys * 2, (legacy, phys, log)
m = re.search(r'(\d+)-Core', hw['cpu_model'])
if m:
    assert int(m.group(1)) == phys, (hw['cpu_model'], phys)
# A legacy thread count that is a real configuration is preserved; one that is
# not (14/11/7) is re-derived from the model, because coherence wins.
real = {4, 6, 8, 12, 16}
if legacy in real:
    assert log == legacy, ('legacy count should be kept', legacy, log)
print("legacy cpu_cores=%d -> %s (%dP/%dT) OK" % (legacy, hw['cpu_model'], phys, log))
PYEOF
done
echo "legacy upgrade OK"

# 6. A corrupt signature must not abort the script — the profile still launches.
rm -rf "$TMP/corrupt"; mkdir -p "$TMP/corrupt"
printf '{ this is not json' > "$TMP/corrupt/hardware-signature.json"
OUT="$(CHROME_CONFIG_DIR="$TMP/corrupt" CHROME_SPOOF_DIR="$TMP/corrupt/spoof" \
       bash -c 'source "$0" broken >/dev/null; echo "$CHROME_CPU_CORES"' \
       "$ROOT/scripts/docker/hardware-spoof.sh")"
[[ "$OUT" =~ ^[0-9]+$ ]] || { echo "FAIL: corrupt signature broke launch ($OUT)"; exit 1; }
echo "corrupt-signature recovery OK"

# 7. Unwritable config dir must not abort the script.
rm -rf "$TMP/ro"; mkdir -p "$TMP/ro"; chmod 500 "$TMP/ro"
OUT="$(CHROME_CONFIG_DIR="$TMP/ro" CHROME_SPOOF_DIR="$TMP/spoof" \
       bash -c 'source "$0" ro >/dev/null; echo "$CHROME_CPU_CORES"' \
       "$ROOT/scripts/docker/hardware-spoof.sh")"
chmod 700 "$TMP/ro"
[[ "$OUT" =~ ^[0-9]+$ ]] || { echo "FAIL: read-only config dir broke launch ($OUT)"; exit 1; }
echo "read-only config dir OK"

# 8. A missing shim is a warning, never a failure.
ERR="$(CHROME_CONFIG_DIR="$TMP" bash "$ROOT/scripts/docker/hardware-spoof.sh" testprof 2>&1 >/dev/null)"
grep -q "no LD_PRELOAD shim" <<< "$ERR" || { echo "FAIL: expected a warning about the missing shim, got: $ERR"; exit 1; }
echo "missing-shim warning OK"

# 9. Distribution: the model pool must not be a single fingerprint. 40 fresh
#    profiles must produce several distinct machines.
rm -rf "$TMP/dist"; mkdir -p "$TMP/dist"
for i in $(seq 1 40); do
  d="$TMP/dist/p$i"; mkdir -p "$d"
  CHROME_CONFIG_DIR="$d" CHROME_SPOOF_DIR="$d/spoof" \
      bash "$ROOT/scripts/docker/hardware-spoof.sh" "p$i" >/dev/null 2>/dev/null
done
python3 - "$TMP/dist" <<'PYEOF'
import glob, json, os, sys
models, boards, counts, macs = set(), set(), set(), set()
for f in glob.glob(os.path.join(sys.argv[1], 'p*', 'hardware-signature.json')):
    h = json.load(open(f))['hardware']
    models.add(h['cpu_model']); boards.add(h['dmi']['product_name'])
    counts.add(h['cpu_cores']); macs.add(h['mac_address'])
assert len(models) >= 4, 'model pool too small: %d' % len(models)
assert len(boards) >= 4, 'board pool too small: %d' % len(boards)
assert len(counts) >= 3, 'core counts too uniform: %d' % len(counts)
assert len(macs) == 40, 'MAC addresses collide: %d unique of 40' % len(macs)
print('distribution OK (%d models, %d boards, %d core counts, %d unique MACs)'
      % (len(models), len(boards), len(counts), len(macs)))
PYEOF

echo "ALL HARDWARE-SPOOF TESTS PASSED"
