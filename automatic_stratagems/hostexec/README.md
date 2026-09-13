# hostexec

These files run as scripts under the host `/usr/bin/python3`, outside the
Flatpak sandbox. They may import only the standard library,
`automatic_stratagems.shared`, and sibling `automatic_stratagems.hostexec`
modules.

Each script inserts the plugin root on `sys.path` exactly once, before its
first repository import.

`automatic_stratagems/tools/check-imports` enforces the import rule.
`tests/test_hostexec_scripts.py` runs every script from `/` with `-I -S`, which
proves the bootstrap works and exercises each script's module-level imports.
It does not cover imports inside functions; `check-imports` covers those.

Never decode images or do recognition here.
