
# Note: Windows stores virtual environment scripts under Scripts/ directory instead of bin/
venv_bin := if os_family() == "windows" { ".venv/Scripts" } else { ".venv/bin" }

# Install python virtual environment (editable install with all adapters)
venv:
    [ -d .venv ] || python3 -m venv .venv
    {{venv_bin}}/python3 -m pip install -e ".[all]"

# Run tests. The compose adapter tests require a running docker daemon with
# the compose v2 plugin (they are skipped if docker is not available)
test:
    {{venv_bin}}/python3 -m unittest -v
