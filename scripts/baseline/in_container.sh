#!/usr/bin/env bash
# Runs inside the python:<version> container, started by run_bug.py.
#
# /bug/buggy/<project>  /bug/fixed/<project>  the two checkouts (read-write)
# /bug/buggy/           /bug/fixed/           logs and exit codes, per version, and
#                                             repairs.txt: each gap filled, one a line
# /bugsinpy/framework                         BugsInPy's scripts (read-only)
#
# One environment serves both versions, so a test that fails on buggy and
# passes on fixed differs only in the code:
#   1. bugsinpy-compile builds it in the buggy tree: venv, requirements, setup.sh.
#   2. The buggy tree's tests run.
#   3. The fixed tree reruns what reinstalls the project itself, as
#      bugsinpy-compile does: setup.sh (C extensions are built in the tree,
#      pip install -e . points at it), then the requirements again.
#   4. The fixed tree's tests run.
# pip freeze is saved before each test run, so any drift shows up.
#
# Test commands run here, not through bugsinpy-test, so each keeps its exit
# code. bugsinpy-test only guesses pass/fail from the output text.

set -u
project="$1"
buggy="/bug/buggy/$project"
fixed="/bug/fixed/$project"

# Progress goes to stdout, which run_bug.py prints as it arrives. Everything
# else goes to the log files.
stage() { echo "[$(date +%T)] $*"; }

# Note a gap filled in BugsInPy's environment, for run_bug.py's "repairs".
repaired() { echo "$2" >> "$1/repairs.txt"; }

# Run "$@" with its output in $log, in the background, and report on the log
# every 60 seconds, so long builds (pandas, matplotlib) show they are moving.
with_progress() {
    local log="$1"; shift
    "$@" > "$log" 2>&1 &
    local pid=$! start=$SECONDS next=60 built last
    while kill -0 "$pid" 2>/dev/null; do
        sleep 1
        if (( SECONDS - start >= next )); then
            next=$((next + 60))
            built=$(grep -c "^building '" "$log" 2>/dev/null)
            last=$(grep -v '^\s*$' "$log" 2>/dev/null | tail -1 | cut -c1-90)
            echo "    ... $(( (SECONDS - start) / 60 ))m, ${built} extension builds so far; last: ${last}"
        fi
    done
    wait "$pid"
}

# bugsinpy-compile appends PYTHONPATH to ~/.bashrc and reads it back.
mkdir -p "$HOME"
touch "$HOME/.bashrc"

# Build C extensions in parallel (pandas, matplotlib). distutils reads
# ~/.pydistutils.cfg for every setup.py command, including the ones pip runs,
# so BugsInPy's setup.sh stays as it is.
printf '[build_ext]\nparallel = %s\n' "$BUILD_JOBS" > "$HOME/.pydistutils.cfg"

# bugsinpy-compile runs dos2unix on requirements.txt. The old Python images do
# not ship it, so add a stand-in that does what dos2unix does to these files:
# 117 are UTF-16 (black, fastapi, luigi, sanic, scrapy), which dos2unix turns
# into UTF-8, and many have CRLF line endings.
if ! command -v dos2unix >/dev/null; then
    mkdir -p /tmp/bin
    cat > /tmp/bin/dos2unix.py <<'PYEOF'
import sys
if sys.argv[1:] == ["--version"]:
    sys.exit(print("dos2unix stand-in (python)"))
for path in sys.argv[1:]:
    data = open(path, "rb").read()
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = data.decode("utf-16")
    else:
        text = data.decode("utf-8", "surrogateescape").lstrip("\ufeff")
    with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
        f.write(text.replace("\r\n", "\n"))
PYEOF
    printf '#!/bin/sh\nexec python3 /tmp/bin/dos2unix.py "$@"\n' > /tmp/bin/dos2unix
    chmod +x /tmp/bin/dos2unix
    export PATH="/tmp/bin:$PATH"
fi

# 166 requirements.txt files are a pip freeze that installs the project itself
# from GitHub, e.g. "-e git+https://github.com/psf/black.git@<commit>#egg=black"
# (161 at the buggy commit). That adds a second copy of the project, and in the
# fixed step the requirements reinstall would point the env back at it, so
# fixed tests could run buggy code. Install the tree under test instead.
# (Written as --editable=. because bugsinpy-compile passes each line to pip as
# one argument via xargs: "-e ." arrives as -e with the value " ." and fails,
# which is also why the original -e git+... lines never installed.)
#
# When setup.sh already builds or installs the project (pandas build_ext,
# sanic build, cookiecutter develop: 67 bugs), the line is dropped instead.
# An editable install there only repeats setup.sh's build, and pip's build of
# it runs serially, adding ~7 minutes per pandas tree each time it runs.
SETUP_BUILDS='setup\.py\s+(build_ext|build|install|develop)\b|pip3?\s+install\s+(-[a-z]*e[a-z]*|--editable)(\s+|=)\.(\s|$)|pip3?\s+install\s+\.(\s|$)'
use_tree_for_self_install() {
    local dir="$1" self="^-e git\+.*#egg=${project}\s*$"
    dos2unix "$dir/bugsinpy_requirements.txt"
    grep -qiE "$self" "$dir/bugsinpy_requirements.txt" || return 0
    if [ -f "$dir/bugsinpy_setup.sh" ] && tr -d '\r' < "$dir/bugsinpy_setup.sh" | grep -qiE "$SETUP_BUILDS"; then
        sed -i -E "/$self/Id" "$dir/bugsinpy_requirements.txt"
        repaired "$(dirname "$dir")" "requirements installed the project from GitHub; dropped it, setup.sh builds this tree"
    else
        sed -i -E "s|$self|--editable=.|I" "$dir/bugsinpy_requirements.txt"
        repaired "$(dirname "$dir")" "requirements installed the project from GitHub; installed this tree (--editable=.) instead"
    fi
}
use_tree_for_self_install "$buggy"
use_tree_for_self_install "$fixed"

# Unpinned setup.sh installs (pip install numpy, cython, Twisted, Pillow, ...)
# would get today's releases, e.g. C extensions built against a 2025 numpy.
# Every pip call is constrained to the bug's own requirements.txt pins, so
# they get the versions BugsInPy recorded. That covers 534 of the 664 unpinned
# setup.sh installs; the rest are pinned nowhere and still get the newest.
# The project itself is left out, so its own install is never blocked.
python3 - "$buggy/bugsinpy_requirements.txt" "$project" > /bug/constraints.txt <<'PYEOF'
import re
import sys

path, project = sys.argv[1], sys.argv[2]
norm = lambda name: re.sub(r"[-_.]+", "-", name).lower()
for line in open(path, encoding="utf-8", errors="replace"):
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.\-]*)(\[[^\]]*\])?==([^\s;#]+)", line.strip())
    if m and norm(m.group(1)) != norm(project):
        print(f"{m.group(1)}=={m.group(3)}")
PYEOF
export PIP_CONSTRAINT=/bug/constraints.txt

# Fill the gaps in BugsInPy's environment, after the project is installed.
add_missing() {
    local log="$1" dir="$2"
    if [ -n "$EXTRA_REQUIREMENTS" ]; then
        echo "+ pip install $EXTRA_REQUIREMENTS (extra_requirements.json)" >> "$log"
        # shellcheck disable=SC2086  # a space-separated list of specs
        pip install $EXTRA_REQUIREMENTS >> "$log" 2>&1
        for spec in $EXTRA_REQUIREMENTS; do repaired "$dir" "installed $spec (extra_requirements.json)"; done
    fi
    # 33 bugs (PySnooper, matplotlib) run pytest but never install it: BugsInPy
    # assumes it is already there. Install the version most other bugs pin,
    # unless the bug pins one itself (then the constraint picks it).
    if grep -qE 'pytest|py\.test' bugsinpy_run_test.sh && ! python -c 'import pytest' 2>/dev/null; then
        local spec="$FALLBACK_PYTEST"
        grep -qiE '^pytest==' /bug/constraints.txt && spec="pytest"
        echo "+ pip install $spec (not installed by requirements or setup.sh)" >> "$log"
        pip install "$spec" >> "$log" 2>&1
        repaired "$dir" "installed $spec: nothing else installs pytest"
    fi
}

# Commands are split on newlines and ";", as bugsinpy-test does: one matplotlib
# bug puts two pytest commands on one line, and each needs its own exit code.
run_tests() {
    local dir="$1" i=0 line cmd
    while IFS= read -r line || [ -n "$line" ]; do
        IFS=';' read -r -a cmds <<< "${line%$'\r'}"
        for cmd in "${cmds[@]}"; do
            [ -z "${cmd// /}" ] && continue
            i=$((i + 1))
            printf '%s\n' "$cmd" > "$dir/test_$i.cmd"
            stage "  $ $cmd"
            timeout "$TEST_TIMEOUT" bash -c "$cmd" > "$dir/test_$i.log" 2>&1 < /dev/null
            echo $? > "$dir/test_$i.exit"
            stage "    exit $(cat "$dir/test_$i.exit")"
        done
    done < bugsinpy_run_test.sh
}

# 1. Build the environment in the buggy tree.
cd "$buggy"
stage "compiling the buggy tree (bugsinpy-compile, $BUILD_JOBS build jobs) -> buggy/compile.log"
with_progress /bug/buggy/compile.log \
    timeout "$COMPILE_TIMEOUT" bash /bugsinpy/framework/bin/bugsinpy-compile -w "$buggy"
if [ ! -f env/bin/activate ]; then
    echo "no env/bin/activate after compile" >> /bug/buggy/compile.log
    stage "compile made no environment; stopping"
    exit 0
fi
source env/bin/activate

# Same PYTHONPATH bugsinpy-compile sets up (31 bugs: ansible, cookiecutter,
# tqdm), built from bug.info's pythonpath the way it does: each ";"-separated
# entry's first folder (the project name) becomes the work dir, so
# "ansible/build/lib/" -> /bug/buggy/ansible/build/lib/.
# It is not read back from ~/.bashrc: in a fresh home, bugsinpy-compile writes
# the literal line PYTHONPATH="$pythonpath_set:$PYTHONPATH" there (single
# quotes), which only means something inside bugsinpy-compile's own shell.
buggy_pythonpath=""
entries=$(sed -n 's/^pythonpath="\(.*\)".*/\1/p' bugsinpy_bug.info | tr -d '\r')
IFS=';' read -r -a entries <<< "$entries"
for entry in "${entries[@]}"; do
    [ -z "$entry" ] && continue
    path=$(echo "$buggy/${entry#*/}" | sed 's#//*#/#g')
    buggy_pythonpath="${buggy_pythonpath:+$buggy_pythonpath:}$path"
done

# 2. Test the buggy tree.
add_missing /bug/buggy/compile.log /bug/buggy
pip freeze > /bug/buggy/freeze.txt 2>/dev/null
echo "$buggy_pythonpath" > /bug/buggy/pythonpath
export PYTHONPATH="$buggy_pythonpath"
stage "testing the buggy tree"
run_tests /bug/buggy

# 3. Reinstall the project from the fixed tree, as bugsinpy-compile would:
# setup.sh's lines (split on ";" too), then the requirements again.
cd "$fixed"
export PYTHONPATH=""   # as during bugsinpy-compile's own setup step
cat > /tmp/setup_fixed.sh <<'EOF'
dos2unix bugsinpy_requirements.txt   # as bugsinpy-compile did for the buggy tree
run_setup_all=""
if [ -f bugsinpy_setup.sh ]; then
    while IFS= read -r line || [ -n "$line" ]; do run_setup_all+="$line;"; done < bugsinpy_setup.sh
fi
IFS=';' read -r -a run_setup <<< "$run_setup_all"
for cmd in "${run_setup[@]}"; do
    cmd=$(echo $cmd | sed -e 's/\r//g')
    [ -n "$cmd" ] && $cmd
done
if grep -q '[^[:space:]]' bugsinpy_requirements.txt; then
    sed -e 's/\r$//' -e '/^\s*#.*$/d' -e '/^\s*$/d' bugsinpy_requirements.txt | xargs -I {} pip install {}
fi
EOF
stage "reinstalling the project from the fixed tree -> fixed/setup.log"
with_progress /bug/fixed/setup.log timeout "$COMPILE_TIMEOUT" bash /tmp/setup_fixed.sh
echo $? > /bug/fixed/setup.exit

# 4. Test the fixed tree, with PYTHONPATH pointing into it instead.
add_missing /bug/fixed/setup.log /bug/fixed
pip freeze > /bug/fixed/freeze.txt 2>/dev/null
fixed_pythonpath="${buggy_pythonpath//\/bug\/buggy\//\/bug\/fixed\/}"
echo "$fixed_pythonpath" > /bug/fixed/pythonpath
export PYTHONPATH="$fixed_pythonpath"
stage "testing the fixed tree"
run_tests /bug/fixed
stage "done"
