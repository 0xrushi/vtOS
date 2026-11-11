#
# meshcore: contacts, identity and radio settings for `meshchat --meshcore`.
# License: MIT
#
# Usage: meshcore id | list | import <link> | rm <name> | radio [freq bw sf cr [power]]
#        meshcore name <name> | channel <name> <32 hex digits>
#
# Edits /flash/.meshcore.json only (no radio needed). The identity PRIVATE KEY
# lives in that file: see docs/SECRETS.md.
#

import os

import mcmesh


def main(env, args):
    state = mcmesh.State.load()
    lines, changed = mcmesh.run_cli(state, list(args), os.urandom)
    for line in lines:
        print(line)
    if changed:
        state.save()
