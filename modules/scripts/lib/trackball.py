"""Route trackball input to the active application or local scrollback."""

import tdeck_trk


def poll(env):
    horizontal = tdeck_trk.get_scroll_horiz()
    if abs(horizontal) > 1:
        env.kvm.inject("\x1b[D" if horizontal < 0 else "\x1b[C")

    vertical = tdeck_trk.get_scroll_vert()
    if abs(vertical) > 1:
        if getattr(env, "ssh_scrollback", False):
            # Plain SSH output, including Claude's screen-reader interface,
            # lives in this terminal's history. Arrow keys go to the remote
            # prompt instead and must not be used to scroll that history.
            if vertical > 0:
                env.term.scrollup()
            else:
                env.term.scrolldown()
        else:
            env.kvm.inject("\x1b[A" if vertical > 0 else "\x1b[B")

    # The native driver raises KeyboardInterrupt for a long click.
    if tdeck_trk.get_click():
        env.kvm.inject("\x1b")
