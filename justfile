
# Note: Windows stores virtual environment scripts under Scripts/ directory instead of bin/
venv_bin := if os_family() == "windows" { ".venv/Scripts" } else { ".venv/bin" }

# Install python virtual environment
venv:
    [ -d .venv ] || python3 -m venv .venv
    {{venv_bin}}/pip3 install .

# Run unit tests
test:
    {{venv_bin}}/python3 -m unittest -v
