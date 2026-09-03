#!/usr/bin/env bash
# Tests for scripts/docker/hardware-spoof.sh — run with: bash tests/test_hardware_spoof.sh
# Uses CHROME_CONFIG_DIR to redirect the signature file away from the container path.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

SIG="$TMP/hardware-signature.json"

# 1. First run generates a full signature
CHROME_CONFIG_DIR="$TMP" bash "$ROOT/scripts/docker/hardware-spoof.sh" testprof >/dev/null
[[ -f "$SIG" ]] || { echo "FAIL: signature not created"; exit 1; }
python3 - "$SIG" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
assert d['profile'] == 'testprof', d
assert 2 <= d['hardware']['cpu_cores'] <= 16, d
assert d['hardware']['screen_resolution'] in ('1920x1080', '2560x1440', '1366x768', '1440x900'), d
assert d['system']['timezone'], d
assert d['browser']['language'], d
assert d['browser']['gpu_mode'] in ('vulkan', 'swiftshader'), d
print("signature schema OK")
PYEOF

# 2. Second run is stable — no regeneration
cp "$SIG" "$TMP/sig1.json"
CHROME_CONFIG_DIR="$TMP" bash "$ROOT/scripts/docker/hardware-spoof.sh" testprof >/dev/null
diff "$TMP/sig1.json" "$SIG" >/dev/null \
  && echo "stability OK" \
  || { echo "FAIL: signature changed between runs"; exit 1; }

# 3. Backfill — a field removed from an old signature is regenerated
python3 - "$SIG" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
del d['browser']['gpu_mode']
json.dump(d, open(sys.argv[1], 'w'))
PYEOF
CHROME_CONFIG_DIR="$TMP" bash "$ROOT/scripts/docker/hardware-spoof.sh" testprof >/dev/null
python3 - "$SIG" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
assert 'gpu_mode' in d['browser'], 'backfill failed'
print("backfill OK")
PYEOF

# 4. Exported values match the stored signature
OUT="$(CHROME_CONFIG_DIR="$TMP" bash -c 'source "$0" testprof >/dev/null; echo "$CHROME_CPU_CORES|$CHROME_RESOLUTION|$CHROME_TIMEZONE|$CHROME_LANGUAGE|$CHROME_GPU_MODE"' "$ROOT/scripts/docker/hardware-spoof.sh")"
python3 - "$SIG" "$OUT" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
cores, res, tz, lang, gpu = sys.argv[2].split('|')
assert int(cores) == d['hardware']['cpu_cores'], (cores, d)
assert res == d['hardware']['screen_resolution'], (res, d)
assert tz == d['system']['timezone'], (tz, d)
assert lang == d['browser']['language'], (lang, d)
assert gpu == d['browser']['gpu_mode'], (gpu, d)
print("exports OK")
PYEOF

echo "ALL HARDWARE-SPOOF TESTS PASSED"