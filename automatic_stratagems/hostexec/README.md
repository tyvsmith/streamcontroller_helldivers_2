# hostexec

These files run as scripts under the host `/usr/bin/python3`, outside the
Flatpak sandbox. They may import only the standard library,
`automatic_stratagems.shared`, and sibling `automatic_stratagems.hostexec`
modules.

Each script has exactly one unconditional
`sys.path.insert(0, <plugin root>)` before its first repository import.
This is enforced by `automatic_stratagems/tools/check-imports`.

Never decode images or do recognition here.
